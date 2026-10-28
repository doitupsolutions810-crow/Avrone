"""Offline tests for the deny-by-default `priv_admin` tool, and for the wiring that carries a granted
capability from fleet.handle_run to the tools spawned in that run (and ONLY that run).

No real network or shell: fleet nodes are driven through handle_run directly, the runner is fake,
the CodeX is a real one on tmp_path, the queue is a stub.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.fleet import FleetManager, GatewayRefused
from avrone.sovereign import CommandResult
from avrone.tools import builtin_registry

ENV = ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
       "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW",
       "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_PRIVILEGED_MAX_HOPS",
       "AVRONE_PRIV_SHELL_ALLOW", "AVRONE_PRIV_SHELL_TIMEOUT")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


class FakeRunner:
    kind = "local"
    workspace = "/nonexistent"

    def __init__(self):
        self.calls = []

    async def run(self, cmd, timeout=None):
        self.calls.append(cmd)
        return CommandResult(cmd, 0, "ran\n", "")


class StubQueue:
    async def stats(self):
        return {"queued": 2, "done": 5}


def make_core(codex=None):
    c = MagicMock()
    c.active = {}
    c.tools.names.return_value = []
    c.runner = FakeRunner()
    c.codex = codex or MagicMock()
    c.fleet_hops = 0
    c.fleet_privileged = ""
    return c


def make_ctx(priv="", grant=None, core=None, run_id="r1"):
    core = core or make_core()
    core.fleet_privileged = priv
    g = priv if grant is None else grant
    return SimpleNamespace(core=core, name="Ops-1", run_id=run_id, depth=0, grant=g)


def admin(ctx, action, **kw):
    with tx.priv_grant(ctx.grant):
        return run(tx.t_priv_admin(ctx, {"action": action, **kw}))


def events(core, kind):
    return [c for c in core.codex.emit.call_args_list if c.args[1] == kind]


def node(core, tmp_path, monkeypatch, nid="edge", toggle="allow", listed="router"):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", nid)
    if toggle is not None:
        monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED", toggle)
    else:
        monkeypatch.delenv("AVRONE_FLEET_PRIVILEGED", raising=False)
    if listed is not None:
        monkeypatch.setenv("AVRONE_PRIVILEGED_NODES", listed)
    else:
        monkeypatch.delenv("AVRONE_PRIVILEGED_NODES", raising=False)
    d = tmp_path / nid
    d.mkdir(exist_ok=True)
    fm = FleetManager(core, d)
    core.fleet = fm
    return fm


def priv_payload(cap="priv:admin", **over):
    return {"route": [], "mode": "chat", "prompt": "p", "hops": 1, "from": "router",
            "privileged": True, "capability": cap, **over}


# ================================================================ priv_admin: deny by default
@pytest.mark.parametrize("priv,grant", [
    ("", None), ("priv:shell", None), ("PRIV:ADMIN", None), (None, None),
    ("priv:admin", ""),            # core flag without the run-scoped grant (another run's privilege)
    ("", "priv:admin"),            # run-scoped grant without the core flag
])
def test_admin_refused_without_granted_admin_capability(priv, grant):
    ctx = make_ctx(priv, grant if grant is not None else (priv if isinstance(priv, str) else ""))
    ctx.core.fleet = MagicMock()
    out = admin(ctx, "node_list")
    assert out.startswith("ERROR: requires a granted priv:admin capability")
    ev = events(ctx.core, "admin.refused")
    assert len(ev) == 1 and ev[0].kwargs["code"] == "no_capability" and ev[0].kwargs["action"] == "node_list"
    assert not events(ctx.core, "admin.action")
    ctx.core.fleet.list_nodes.assert_not_called()


@pytest.mark.parametrize("action", ["", "shell", "exec", "eval", "__import__", "queue_purge", "NODE_LIST",
                                    "node_list; rm -rf /"])
def test_admin_unknown_action_refused_even_when_granted(action):
    ctx = make_ctx("priv:admin")
    out = admin(ctx, action)
    assert out.startswith("ERROR: unknown admin action") and "node_list" in out
    assert events(ctx.core, "admin.refused")[0].kwargs["code"] == "unknown_action"
    assert ctx.core.runner.calls == []


def test_admin_action_set_is_small_explicit_and_read_only():
    assert set(tx.PRIV_ADMIN_ACTIONS) == {"queue_stats", "node_list", "fleet_info", "codex_runs", "codex_tail"}


# ================================================================ priv_admin: the allowed actions
def test_admin_queue_stats_and_node_list(tmp_path, monkeypatch):
    core = make_core()
    fm = node(core, tmp_path, monkeypatch)
    fm.nodes["c1"] = {"node_id": "c1", "url": "http://c1", "last_seen": 0, "load": 3,
                      "capabilities": {"runner": "docker", "secret": "x"}, "children": []}
    ctx = make_ctx("priv:admin", core=core)
    out = admin(ctx, "queue_stats")
    assert out == "ERROR: queue not running"
    assert events(core, "admin.action")[-1].kwargs == {"tool": "priv_admin", "action": "queue_stats",
                                                       "ok": False, "error": "LookupError: queue not running"}
    fm.queue = StubQueue()
    assert json.loads(admin(ctx, "queue_stats")) == {"queued": 2, "done": 5}
    nodes = json.loads(admin(ctx, "node_list"))
    assert nodes == [{"node_id": "c1", "status": "offline", "load": 3, "children": [], "last_seen": 0,
                      "runner": "docker"}]
    assert "url" not in nodes[0] and "secret" not in json.dumps(nodes)
    assert json.loads(admin(ctx, "fleet_info"))["node_id"] == "edge"
    oks = [e.kwargs["action"] for e in events(core, "admin.action") if e.kwargs["ok"]]
    assert oks == ["queue_stats", "node_list", "fleet_info"]
    assert core.runner.calls == []


def test_admin_codex_runs_and_tail(tmp_path):
    codex = Codex(tmp_path)
    for i in range(30):
        codex.emit("run-a", "tool.call", "Ops-1", i=i)
    core = make_core(codex)
    ctx = make_ctx("priv:admin", core=core, run_id="run-a")
    runs = json.loads(admin(ctx, "codex_runs", limit=999))
    assert [r["run_id"] for r in runs] == ["run-a"]
    tail = json.loads(admin(ctx, "codex_tail", limit=5))  # default run_id = this run
    assert len(tail) == 5 and tail[-1]["kind"] == "admin.action"  # the codex_runs audit, logged to this run
    assert json.loads(tail[-2]["data"]) == {"i": 29}
    kinds = [e["kind"] for e in codex.replay("run-a")]
    assert kinds.count("admin.action") == 2  # the tool audits into the real CodeX too
    assert admin(ctx, "codex_tail", run_id="../etc").startswith("ERROR: invalid run_id")
    assert json.loads(admin(ctx, "codex_tail", run_id="nope")) == []


def test_admin_registered_and_refuses_through_registry():
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    assert {"priv_admin", "priv_shell"} <= set(reg.names())
    ctx = make_ctx("")
    with tx.priv_grant(""):
        out = run(reg.call(ctx, "priv_admin", {"action": "node_list"}))
    assert out.startswith("ERROR: requires a granted priv:admin capability")


# ================================================================ handle_run -> tools wiring
async def _agent_tools(core, run_id="r", **tool_args):
    """What an agent in the run does: spawn sub-agents (child tasks) that call the privileged tools."""
    async def sub(name):
        ctx = SimpleNamespace(core=core, name=name, run_id=run_id, depth=1)
        return (await tx.t_priv_shell(ctx, {"cmd": "ls -la"}),
                await tx.t_priv_admin(ctx, {"action": "fleet_info"}),
                tx.granted_capability(ctx))
    return await asyncio.gather(*(asyncio.create_task(sub(f"Sub-{i}")) for i in range(2)))


def test_granted_shell_capability_reaches_tools_spawned_in_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    core = make_core()
    fm = node(core, tmp_path, monkeypatch)
    seen = {}

    async def chat(prompt):
        seen["out"] = await _agent_tools(core)
        return {"run_id": "r", "answer": "ok"}
    core.chat = chat
    assert run(fm.handle_run(priv_payload("priv:shell")))["answer"] == "ok"
    for shell_out, admin_out, cap in seen["out"]:
        assert cap == "priv:shell" and "ran" in shell_out
        assert admin_out.startswith("ERROR: requires a granted priv:admin")  # shell grant != admin grant
    assert core.runner.calls == ["ls -la", "ls -la"]
    assert core.fleet_privileged == "" and tx.PRIV_GRANT.get() == ""  # scope ends with the run
    with tx.priv_grant(""):
        after = run(tx.t_priv_shell(make_ctx(core=core), {"cmd": "ls"}))
    assert after.startswith("ERROR: requires a granted priv:shell")


def test_granted_admin_capability_reaches_tools_spawned_in_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    core = make_core()
    fm = node(core, tmp_path, monkeypatch)
    seen = {}

    async def chat(prompt):
        seen["out"] = await _agent_tools(core)
        return {"run_id": "r", "answer": "ok"}
    core.chat = chat
    run(fm.handle_run(priv_payload("priv:admin")))
    for shell_out, admin_out, cap in seen["out"]:
        assert cap == "priv:admin" and json.loads(admin_out)["node_id"] == "edge"
        assert shell_out.startswith("ERROR: requires a granted priv:shell")
    assert core.runner.calls == []


def test_unprivileged_run_tools_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    core = make_core()
    fm = node(core, tmp_path, monkeypatch)
    seen = {}

    async def chat(prompt):
        seen["out"] = await _agent_tools(core)
        return {"answer": "ok"}
    core.chat = chat
    run(fm.handle_run({"mode": "chat", "prompt": "p", "hops": 1, "from": "router", "privileged": False}))
    for shell_out, admin_out, cap in seen["out"]:
        assert cap == "" and shell_out.startswith("ERROR: requires") and admin_out.startswith("ERROR: requires")
    assert core.runner.calls == []
    assert len(events(core, "priv.refused")) == 2 and len(events(core, "admin.refused")) == 2


@pytest.mark.parametrize("toggle,listed", [(None, "router"), ("allow", None), ("allow", "someone-else")])
def test_gate_refusal_means_no_tool_ever_runs(tmp_path, monkeypatch, toggle, listed):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    core = make_core()
    fm = node(core, tmp_path, monkeypatch, toggle=toggle, listed=listed)

    async def chat(prompt):
        pytest.fail("a refused privileged run must never reach the agents")
    core.chat = chat
    with pytest.raises(GatewayRefused):
        run(fm.handle_run(priv_payload("priv:shell")))
    assert core.runner.calls == [] and core.fleet_privileged == ""


def test_concurrent_unrelated_run_does_not_inherit_the_grant(tmp_path, monkeypatch):
    """While a privileged run is live on the core, a run started elsewhere on the same core (UI chat,
    queue worker) sees core.fleet_privileged set -- but has no run-scoped grant, so stays refused."""
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    core = make_core()
    fm = node(core, tmp_path, monkeypatch)
    results = {}

    async def main():
        inside, release = asyncio.Event(), asyncio.Event()

        async def chat(prompt):
            inside.set()
            await release.wait()
            results["priv"] = await _agent_tools(core)
            return {"answer": "ok"}
        core.chat = chat

        async def bystander():
            await inside.wait()
            assert core.fleet_privileged == "priv:shell"  # the core-wide flag IS visible...
            ctx = SimpleNamespace(core=core, name="UI-1", run_id="ui", depth=0)
            results["other"] = await tx.t_priv_shell(ctx, {"cmd": "ls"})  # ...but the grant is not
            release.set()
        other = asyncio.create_task(bystander())  # created before the privileged run's context is set
        await fm.handle_run(priv_payload("priv:shell"))
        await other
    run(main())
    assert results["other"].startswith("ERROR: requires a granted priv:shell capability")
    assert all("ran" in s for s, _, _ in results["priv"])
    assert core.runner.calls == ["ls -la", "ls -la"]
