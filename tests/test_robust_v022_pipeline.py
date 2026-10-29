"""v0.22 robust: END-TO-END "linking pipeline" for the privileged (root-enabled) fleet tier.

A real signed request is driven through the whole chain, entirely offline:

    sign() -> FleetManager.dispatch_gateway -> FleetManager.handle_run -> TrinityCore.chat
           -> run_agent (scripted fake LLM returns a tool call) -> ToolRegistry.call
           -> priv_shell / priv_admin -> fake runner

and proves: (a) with every condition met the allow-listed command runs and is audited; (b) removing
ANY ONE condition refuses with its distinct code and nothing executes; (c) the run-scoped grant does
not leak to a concurrent sibling run or past the end of the run.
"""
import asyncio
import json
import time

import pytest

from avrone import fleet as fleet_mod
from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.config import Settings
from avrone.fleet import FleetManager, GatewayRefused, sign
from avrone.sovereign import CommandResult
from avrone.tools import builtin_registry
from avrone.trinity import TrinityCore

PATH = "/api/fleet/run"
TOKEN = "pipeline-tok"
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
    """Records every command the host would have executed. Nothing ever runs for real."""
    kind = "local"
    workspace = "/nonexistent"

    def __init__(self):
        self.calls: list[str] = []

    async def run(self, cmd, timeout=None, cwd=None):
        self.calls.append(cmd)
        await asyncio.sleep(0)  # an await point inside the privileged action
        return CommandResult(cmd, 0, f"RAN<{cmd}>\n", "")


class ToolLLM:
    """Fake LLM: the first step of an agent whose task contains a key in `script` returns that tool
    call; after a tool result comes back it finishes, echoing the last tool output. Keyed by task
    so concurrent runs on one core each get their own script."""

    def __init__(self, script: dict[str, dict]):
        self.script = script
        self.seen: list[str] = []

    async def chat(self, messages, **kw):
        await asyncio.sleep(0)  # interleave concurrent agents at every model call
        text = "\n".join(str(m.get("content", "")) for m in messages)
        self.seen.append(text)
        last = str(messages[-1].get("content", ""))
        if len(messages) > 2 and ("RAN<" in last or "ERROR" in last or "{" in last):
            return json.dumps({"final": "TOOL-OUTPUT: " + last[-400:]})
        for key, call in self.script.items():
            if key in text:
                return json.dumps(call)
        return json.dumps({"final": "no tool"})


def make_core(tmp_path, script):
    s = Settings()
    s.workspace = tmp_path / "ws"
    s.data_dir = tmp_path / "data"
    s.max_steps = 4
    s.agents_can_shell = False  # the ordinary shell is off: only the privileged tier could run anything
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = TrinityCore(s, ToolLLM(script), FakeRunner(), Codex(s.data_dir), tools=reg)
    core.fleet_privileged = ""
    return core


GOOD_ENV = {
    "AVRONE_FLEET_TOKEN": TOKEN,
    "AVRONE_NODE_ID": "edge",
    "AVRONE_FLEET_ALLOW": "router",
    "AVRONE_FLEET_PRIVILEGED": "allow",
    "AVRONE_PRIVILEGED_NODES": "router",
    "AVRONE_PRIV_SHELL_ALLOW": "uptime,ls",
}


def make_node(core, tmp_path, monkeypatch, **override):
    env = {**GOOD_ENV, **override}
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    d = tmp_path / "node"
    d.mkdir(parents=True, exist_ok=True)
    fm = FleetManager(core, d)
    core.fleet = fm
    return fm


def payload(**over):
    p = {"route": [], "mode": "chat", "prompt": "PRIVTASK: check uptime", "hops": 1, "from": "router",
         "privileged": True, "capability": "priv:shell"}
    p.update(over)
    return {k: v for k, v in p.items() if v is not _DROP}


_DROP = object()


def signed(p, token=TOKEN, ts=None, nonce=None, path=PATH):
    body = json.dumps(p).encode()
    return body, sign(token, "POST", path, body, ts=ts, nonce=nonce)


async def pipeline(fm, body, headers):
    """Exactly what the /api/fleet/run handler does: gateway, then handle_run."""
    p = fm.dispatch_gateway("run", "POST", PATH, body, headers)
    return await fm.handle_run(p)


def all_events(core):
    out = []
    for r in core.codex.runs(200):
        out += core.codex.replay(r["run_id"])
    return out


SHELL_CALL = {"tool": "priv_shell", "args": {"cmd": "uptime"}}
ADMIN_CALL = {"tool": "priv_admin", "args": {"action": "fleet_info"}}


# ================================================================ (a) everything met: it runs, audited
def test_all_conditions_met_runs_allowlisted_command_and_audits(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    res = run(pipeline(fm, *signed(payload())))
    assert core.runner.calls == ["uptime"]
    assert "RAN<uptime>" in res["answer"]
    kinds = [e["kind"] for e in all_events(core)]
    for k in ("fleet.dispatch", "fleet.privileged.accept", "fleet.run", "priv.shell"):
        assert k in kinds, k
    shell = [e for e in all_events(core) if e["kind"] == "priv.shell"]
    assert shell[0]["data"]["argv0"] == "uptime" and shell[0]["data"]["exit_code"] == 0
    accept = [e for e in all_events(core) if e["kind"] == "fleet.privileged.accept"][0]["data"]
    assert accept["sender"] == "router" and accept["capability"] == "priv:shell" and accept["hops"] == 1
    # grant is gone after the run, both halves
    assert core.fleet_privileged == "" and tx.PRIV_GRANT.get() == ""


def test_all_conditions_met_priv_admin_runs_and_audits(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": ADMIN_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    res = run(pipeline(fm, *signed(payload(capability="priv:admin"))))
    assert '"node_id": "edge"' in res["answer"]
    assert core.runner.calls == []  # admin views never touch the runner
    acts = [e for e in all_events(core) if e["kind"] == "admin.action"]
    assert acts and acts[0]["data"]["action"] == "fleet_info" and acts[0]["data"]["ok"] is True


def test_shell_grant_cannot_be_used_for_admin_and_vice_versa(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": ADMIN_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    res = run(pipeline(fm, *signed(payload(capability="priv:shell"))))
    assert "requires a granted priv:admin" in res["answer"]
    core2 = make_core(tmp_path / "b", {"PRIVTASK": SHELL_CALL})
    fm2 = make_node(core2, tmp_path / "b", monkeypatch)
    res2 = run(pipeline(fm2, *signed(payload(capability="priv:admin"))))
    assert "requires a granted priv:shell" in res2["answer"] and core2.runner.calls == []


# ================================================================ (b) drop any ONE condition
def _gateway_refusals(core):
    return [e["data"]["code"] for e in all_events(core) if e["kind"] == "fleet.refused"]


def _assert_nothing_ran(core):
    assert core.runner.calls == []
    kinds = [e["kind"] for e in all_events(core)]
    assert "fleet.run" not in kinds and "priv.shell" not in kinds and "fleet.privileged.accept" not in kinds
    assert not any(e["kind"] == "agent.spawn" for e in all_events(core))
    assert core.fleet_privileged == "" and tx.PRIV_GRANT.get() == ""


def _body_headers(case):
    """(body, headers) for a gateway-level negative case."""
    p = payload()
    body, h = signed(p)
    if case == "no_token_header_sig":
        return body, {k: v for k, v in h.items() if k != "x-avrone-sig"}
    if case == "wrong_hmac_key":
        return signed(p, token="not-the-token")
    if case == "tampered_body":  # signature over a different body: e.g. privileged flag flipped in transit
        return json.dumps({**p, "prompt": "PRIVTASK: other"}).encode(), h
    if case == "stale":
        return signed(p, ts=str(int(time.time()) - 3600))
    if case == "future":
        return signed(p, ts=str(int(time.time()) + 3600))
    if case == "header_flag":
        return body, {**h, "x-avrone-privileged": "true"}
    raise AssertionError(case)


@pytest.mark.parametrize("case,code", [
    ("no_token_header_sig", "bad_signature"),
    ("wrong_hmac_key", "bad_signature"),
    ("tampered_body", "bad_signature"),
    ("stale", "stale"),
    ("future", "stale"),
    ("header_flag", "privileged_unsigned"),
])
def test_auth_layer_refusals_execute_nothing(tmp_path, monkeypatch, case, code):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    with pytest.raises(GatewayRefused) as ei:
        run(pipeline(fm, *_body_headers(case)))
    assert ei.value.code == code
    assert code in _gateway_refusals(core)
    _assert_nothing_ran(core)


def test_receiver_without_token_refuses(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch, AVRONE_FLEET_TOKEN=None)
    with pytest.raises(GatewayRefused) as ei:
        run(pipeline(fm, *signed(payload())))
    assert ei.value.code == "no_token" and ei.value.auth
    _assert_nothing_ran(core)


def test_nonce_reuse_is_refused_and_second_run_executes_nothing(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    body, h = signed(payload())
    run(pipeline(fm, body, h))
    assert core.runner.calls == ["uptime"]
    with pytest.raises(GatewayRefused) as ei:
        run(pipeline(fm, body, h))
    assert ei.value.code == "replay"
    assert core.runner.calls == ["uptime"]  # exactly once
    assert "replay" in _gateway_refusals(core)


def test_concurrent_replay_of_one_signed_request_runs_once(tmp_path, monkeypatch):
    """The same signed request fired 8x concurrently on one loop: exactly one execution."""
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    body, h = signed(payload())

    async def main():
        return await asyncio.gather(*(pipeline(fm, body, h) for _ in range(8)), return_exceptions=True)
    out = run(main())
    ok = [r for r in out if isinstance(r, dict)]
    refused = [r for r in out if isinstance(r, GatewayRefused)]
    assert len(ok) == 1 and len(refused) == 7 and all(r.code == "replay" for r in refused)
    assert core.runner.calls == ["uptime"]


@pytest.mark.parametrize("over,env,code", [
    # sender allowlist
    ({"from": "stranger"}, {"AVRONE_PRIVILEGED_NODES": "router,stranger"}, "sender_not_allowed"),
    ({"from": _DROP}, {}, "sender_missing"),
    # hops: privileged needs exactly 1 hop
    ({"hops": 2}, {}, "privileged_hops"),
    ({"hops": 9}, {}, "hops"),
    ({"hops": -1}, {}, "bad_hops"),
    ({"hops": "1; rm"}, {}, "bad_hops"),
    # capability
    ({"capability": "priv:root"}, {}, "privileged_capability"),
    ({"capability": "spawn:chat"}, {}, "privileged_capability"),  # flag without a privileged cap
    ({"privileged": "true"}, {}, "capability"),  # flag must be a JSON boolean
    ({"privileged": False}, {}, "privileged_capability"),  # privileged cap without the signed flag
    ({"privileged": _DROP}, {}, "privileged_capability"),
    ({"mode": "chat", "capability": "spawn:nope", "privileged": False}, {}, "capability"),
    # node policy
    ({}, {"AVRONE_FLEET_PRIVILEGED": None}, "privileged_disabled"),
    ({}, {"AVRONE_FLEET_PRIVILEGED": "yes"}, "privileged_disabled"),
    ({}, {"AVRONE_FLEET_PRIVILEGED": "allow-ish"}, "privileged_disabled"),
    ({}, {"AVRONE_PRIVILEGED_NODES": None}, "privileged_not_listed"),
    ({}, {"AVRONE_PRIVILEGED_NODES": "someone-else"}, "privileged_not_listed"),
    ({"from": "router/agent-7"}, {"AVRONE_PRIVILEGED_NODES": "router/agent-7"}, "privileged_not_listed"),
])
def test_policy_layer_refusals_execute_nothing(tmp_path, monkeypatch, over, env, code):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch, **env)
    with pytest.raises(GatewayRefused) as ei:
        run(pipeline(fm, *signed(payload(**over))))
    assert ei.value.code == code
    assert not ei.value.auth
    assert code in _gateway_refusals(core)
    _assert_nothing_ran(core)


def test_privileged_refusals_have_their_own_audit_event(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch, AVRONE_PRIVILEGED_NODES="x")
    with pytest.raises(GatewayRefused):
        run(pipeline(fm, *signed(payload())))
    ev = [e for e in all_events(core) if e["kind"] == "fleet.privileged.refused"]
    assert ev and ev[0]["data"]["code"] == "privileged_not_listed"


@pytest.mark.parametrize("allow,cmd,code", [
    (None, "uptime", "allowlist_empty"),
    ("", "uptime", "allowlist_empty"),
    (" , ,", "uptime", "allowlist_empty"),
    ("ls", "uptime", "not_allowlisted"),
    ("uptime", "/usr/bin/uptime", None),  # path form never matches a bare-name allowlist
    ("uptime", "uptime; rm -rf /", None),
    ("uptime", "uptime $(id)", None),
    ("uptime", "UPTIME", "not_allowlisted"),
    ("uptime", "X=1 uptime", None),
])
def test_tool_layer_refusals_execute_nothing(tmp_path, monkeypatch, allow, cmd, code):
    core = make_core(tmp_path, {"PRIVTASK": {"tool": "priv_shell", "args": {"cmd": cmd}}})
    fm = make_node(core, tmp_path, monkeypatch, AVRONE_PRIV_SHELL_ALLOW=allow)
    res = run(pipeline(fm, *signed(payload())))  # gateway accepts: every fleet condition holds
    assert "ERROR" in res["answer"]
    assert core.runner.calls == []
    refused = [e["data"] for e in all_events(core) if e["kind"] == "priv.refused"]
    assert refused, "tool-level refusal must be audited"
    if code:
        assert refused[0]["code"] == code
    assert not any(e["kind"] == "priv.shell" for e in all_events(core))


def test_unprivileged_signed_run_cannot_reach_priv_tools(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    res = run(pipeline(fm, *signed(payload(privileged=False, capability=_DROP))))
    assert "requires a granted priv:shell" in res["answer"]
    assert core.runner.calls == []
    assert [e["data"]["code"] for e in all_events(core) if e["kind"] == "priv.refused"] == ["no_capability"]


def test_local_run_on_same_core_cannot_reach_priv_tools(tmp_path, monkeypatch):
    """A UI /api/chat (no fleet path at all) on the privileged node is refused too."""
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    make_node(core, tmp_path, monkeypatch)
    res = run(core.chat("PRIVTASK local"))
    assert "requires a granted priv:shell" in res["answer"] and core.runner.calls == []


def test_forwarded_privileged_run_is_refused_outbound_beyond_one_hop(tmp_path, monkeypatch):
    """A privileged run that asks this node to forward it (route non-empty) would be hop 2 at the
    next node: refused locally by check_outbound before any network or execution."""
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)
    with pytest.raises(GatewayRefused) as ei:
        run(pipeline(fm, *signed(payload(route=["far"]))))
    assert ei.value.code == "privileged_hops"
    assert core.runner.calls == []


def test_handle_run_regates_without_dispatch_gateway(tmp_path, monkeypatch):
    """Defense in depth: calling handle_run directly with a payload the gateway would refuse."""
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch, AVRONE_PRIVILEGED_NODES="someone-else")
    with pytest.raises(GatewayRefused) as ei:
        run(fm.handle_run(payload()))
    assert ei.value.code == "privileged_not_listed" and core.runner.calls == []
    fm2 = make_node(core, tmp_path, monkeypatch)
    for bad in (payload(privileged="yes"), payload(privileged=1), payload(privileged=False),
                payload(hops=2), payload(capability="priv:root")):
        with pytest.raises(GatewayRefused):
            run(fm2.handle_run(bad))
    assert core.runner.calls == []


# ================================================================ (c) no grant leak across runs
def test_grant_does_not_leak_to_concurrent_sibling_or_after(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"PRIVTASK": SHELL_CALL, "PLAINTASK": SHELL_CALL})
    fm = make_node(core, tmp_path, monkeypatch)

    async def main():
        priv = pipeline(fm, *signed(payload()))
        plain = pipeline(fm, *signed(payload(prompt="PLAINTASK: check uptime", privileged=False,
                                             capability=_DROP)))
        local = core.chat("PLAINTASK from the UI")
        return await asyncio.gather(priv, plain, local)

    for _ in range(5):
        core.runner.calls.clear()
        rp, rq, rl = run(main())
        assert "RAN<uptime>" in rp["answer"]
        assert "requires a granted priv:shell" in rq["answer"]
        assert "requires a granted priv:shell" in rl["answer"]
        assert core.runner.calls == ["uptime"]
        assert core.fleet_privileged == "" and tx.PRIV_GRANT.get() == ""


@pytest.mark.parametrize("order", ["priv_first", "plain_first"])
def test_core_flag_restored_when_overlapping_runs_end_out_of_order(tmp_path, monkeypatch, order):
    """Overlapping runs that START and END in crossed order (A starts, B starts, A ends, B ends).
    The core-wide half of the grant must be "" once every privileged run has ended, and a privileged
    run's tools must keep working while an unprivileged run overlaps it."""
    core = make_core(tmp_path, {})
    fm = make_node(core, tmp_path, monkeypatch)
    seen = {}

    async def main():
        started = {k: asyncio.Event() for k in "AB"}
        finish = {k: asyncio.Event() for k in "AB"}

        async def chat(prompt, run_id=None):
            k = prompt[0]
            started[k].set()
            await finish[k].wait()
            from types import SimpleNamespace
            ctx = SimpleNamespace(core=core, name=k, run_id=k, depth=0)
            seen[k] = await tx.t_priv_shell(ctx, {"cmd": "uptime"})
            return {"answer": seen[k]}
        core.chat = chat
        first, second = ("A", "B")
        priv_key = "A" if order == "priv_first" else "B"
        mk = {k: (payload(prompt=f"{k}") if k == priv_key else
                  payload(prompt=f"{k}", privileged=False, capability=_DROP)) for k in "AB"}
        ta = asyncio.create_task(pipeline(fm, *signed(mk[first])))
        await started[first].wait()
        tb = asyncio.create_task(pipeline(fm, *signed(mk[second])))
        await started[second].wait()
        finish[first].set()
        await ta
        finish[second].set()
        await tb
        seen["flag_after"] = core.fleet_privileged
        seen["priv"] = priv_key
    run(main())
    assert seen["flag_after"] == "", "core-wide privileged flag leaked past the end of every run"
    plain = "B" if seen["priv"] == "A" else "A"
    assert seen[plain].startswith("ERROR: requires a granted priv:shell")
    assert "RAN<uptime>" in seen[seen["priv"]], "privileged run lost its grant while overlapped"


def test_grant_is_not_visible_to_tasks_created_before_the_run(tmp_path, monkeypatch):
    """A long-lived task (queue worker / heartbeat) created before the privileged run never sees the
    run-scoped grant, even while the core-wide flag is up."""
    core = make_core(tmp_path, {})
    fm = make_node(core, tmp_path, monkeypatch)
    from types import SimpleNamespace
    out = {}

    async def main():
        go, done = asyncio.Event(), asyncio.Event()

        async def worker():
            await go.wait()
            out["flag"] = core.fleet_privileged
            out["res"] = await tx.t_priv_shell(SimpleNamespace(core=core, name="W", run_id="w", depth=0),
                                               {"cmd": "uptime"})
            done.set()
        w = asyncio.create_task(worker())

        async def chat(prompt, run_id=None):
            go.set()
            await done.wait()
            return {"answer": "ok"}
        core.chat = chat
        await pipeline(fm, *signed(payload()))
        await w
    run(main())
    assert out["flag"] == "priv:shell"
    assert out["res"].startswith("ERROR: requires a granted priv:shell") and core.runner.calls == []


def test_two_concurrent_privileged_runs_with_different_caps_get_their_own(tmp_path, monkeypatch):
    core = make_core(tmp_path, {"SHELLTASK": SHELL_CALL, "ADMINTASK": ADMIN_CALL})
    fm = make_node(core, tmp_path, monkeypatch)

    async def main():
        return await asyncio.gather(
            pipeline(fm, *signed(payload(prompt="SHELLTASK go"))),
            pipeline(fm, *signed(payload(prompt="ADMINTASK go", capability="priv:admin"))))
    for _ in range(5):
        core.runner.calls.clear()
        rs, ra = run(main())
        # each run may only use ITS capability; the other one's tool stays refused
        assert core.runner.calls in ([], ["uptime"])
        assert "ERROR" in ra["answer"] or '"node_id": "edge"' in ra["answer"]
        assert "requires a granted priv:admin" not in rs["answer"]
        assert core.fleet_privileged == "" and tx.PRIV_GRANT.get() == ""


def test_overlapping_shallow_run_never_lowers_a_deep_runs_hop_count(tmp_path, monkeypatch):
    """core.fleet_hops bounds remote_spawn forwarding. A shallow run (hops=1) starting while a deep
    run (hops=3) is live used to overwrite it with 1 -- the deep run's agents could forward 2 more hops
    -- and crossed-order ends left the core at a stale value after every run had finished."""
    core = make_core(tmp_path, {})
    core.fleet_hops = 0
    fm = make_node(core, tmp_path, monkeypatch, AVRONE_FLEET_MAX_HOPS="5")
    seen = {}

    async def main():
        started = {k: asyncio.Event() for k in "DS"}
        finish = {k: asyncio.Event() for k in "DS"}

        async def chat(prompt, run_id=None):
            k = prompt[0]
            started[k].set()
            await finish[k].wait()
            seen[k] = core.fleet_hops
            return {"answer": "ok"}
        core.chat = chat
        deep = asyncio.create_task(pipeline(fm, *signed(payload(prompt="D", hops=3, privileged=False,
                                                                capability=_DROP))))
        await started["D"].wait()
        shallow = asyncio.create_task(pipeline(fm, *signed(payload(prompt="S", hops=1, privileged=False,
                                                                   capability=_DROP))))
        await started["S"].wait()
        finish["D"].set()
        await deep
        seen["mid"] = core.fleet_hops
        finish["S"].set()
        await shallow
        seen["after"] = core.fleet_hops
    run(main())
    assert seen["D"] >= 3, "deep run's hop bound was lowered by an overlapping shallow run"
    assert seen["S"] >= 1 and seen["mid"] == 1
    assert seen["after"] == 0
