"""v0.25 robustness: a whole-registry fuzz. Every safe tool, fed junk args from any entry point,
returns a string and never surfaces an internal AttributeError/KeyError/TypeError to the model.
Plus: task_board survives a corrupt board, refuses bad ops cleanly, and writes atomically."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit
from avrone.codex import Codex
from avrone.config import Settings
from avrone.llm import LLM
from avrone.sovereign import LocalRunner
from avrone.tools import Tool, ToolRegistry, builtin_registry
from avrone.tools_extra import register_extra_tools
from avrone.trinity import AgentContext, TrinityCore

# tools that run commands, touch the network, or start long jobs: excluded from the fuzz
SKIP = {"shell", "python", "git", "spawn", "spawn_agent", "spawn_squad", "fetch", "web_search", "http_request",
        "env_install", "evolve_start", "evolve_loop", "evolve_propose", "evolve_skill_propose", "remote_spawn",
        "remote_spawn_many", "priv_shell", "priv_admin", "port_scan", "dns_lookup", "dns_enum", "rdap_whois",
        "http_probe", "http_fingerprint", "tls_inspect", "tcp_ping", "traceroute", "ip_geo", "self_scan",
        "cert_transparency", "osint_domain", "osint_metrics", "docker_run", "agent_migrate", "memory_sync",
        "fleet_message", "queue_submit", "flake_probe"}
INTERNAL = ("AttributeError", "KeyError", "IndexError", "NoneType", "RecursionError", "UnboundLocalError")
JUNK = [None, [], "str", 7, {}, {"x": 1},
        {"path": ["a"], "name": {"a": 1}, "key": 10 ** 30, "limit": "-5", "run_id": "../..", "text": None,
         "topic": [1], "op": "add", "id": "x"},
        {k: "\x00" * 3000 for k in ("path", "name", "key", "query", "text", "topic", "goal", "loop_id", "pid",
                                    "node", "agent_id", "run_id", "task", "title")}]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LLM_MODE", "mock")
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.workspace.mkdir(parents=True)
    reg = builtin_registry()
    register_extra_tools(reg)
    core = TrinityCore(s, LLM(s), LocalRunner(s), Codex(s.data_dir), tools=reg)
    return reg, AgentContext(core, "fz", "Fz-1", 0)


def test_registry_fuzz(env):
    reg, ctx = env
    names = [n for n in reg.names() if n not in SKIP]
    assert len(names) >= 40

    async def go():
        bad = []
        for n in names:
            for j in JUNK:
                out = await asyncio.wait_for(reg.call(ctx, n, j), 15)
                if not isinstance(out, str):
                    bad.append((n, repr(j)[:30], type(out).__name__))
                elif out.startswith("ERROR: ") and any(x in out[:60] for x in INTERNAL):
                    bad.append((n, repr(j)[:30], out[:100]))
        return bad
    assert asyncio.run(go()) == []


def test_non_dict_args_never_reach_tool_body():
    seen = []

    async def fn(ctx, args):
        seen.append(args)
        return 42  # non-str return is coerced
    reg = ToolRegistry()
    reg.register(Tool("t", "d", "{}", fn))
    assert asyncio.run(reg.call(None, "t", ["a"])).startswith("ARG ERROR (t): args must be a JSON object, got list")
    assert asyncio.run(reg.call(None, "t", None)) == "42" and seen == [{}]


def _ctx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="W", depth=0)


@pytest.mark.parametrize("body", ["{torn", '{"a": 1}', '[1, "x", {"id": "no"}]'])
def test_task_board_survives_corrupt_board(tmp_path, body):
    c = _ctx(tmp_path)
    agentkit._board(c).write_text(body)
    assert asyncio.run(agentkit.t_task_board(c, {})) == "board empty"
    out = asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "fix it"}))
    assert out.startswith("#1 [todo] fix it")
    assert json.loads(agentkit._board(c).read_text())[0]["title"] == "fix it"


def test_task_board_ids_and_bad_update(tmp_path):
    c = _ctx(tmp_path)
    for t in ("a", "b"):
        asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": t}))
    b = agentkit._board(c)
    b.write_text(json.dumps([json.loads(b.read_text())[1]]))  # only #2 left
    assert "#3 [todo] c" in asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "c"}))
    assert asyncio.run(agentkit.t_task_board(c, {"op": "update", "id": "zz"})) == "ERROR: op=update needs an integer id"
    assert asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "  "})) == "ERROR: op=add needs a title"
    assert not list(Path(b.parent).glob(".*.tmp"))
