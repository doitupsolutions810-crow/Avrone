"""Round 16 skills: fleet-restart-safety and event-stream-health are valid, grounded, and the states
they describe are the ones the tools actually print."""
import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from avrone import evolve
from avrone import tools_gatewayview as gv
from avrone.codex import Codex
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {"fleet-restart-safety": ["replay_guard", "state_health", "fleet_status", "research_note"],
          "event-stream-health": ["stream_status", "codex_search", "codex_stats", "exposure_check", "research_note"]}


def body(n):
    return (ROOT / "skills" / n / "SKILL.md").read_text()


@pytest.mark.parametrize("name", sorted(SKILLS))
def test_valid_and_grounded(name):
    b = body(name)
    assert evolve.check_skill(name, b) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in SKILLS[name]:
        assert t in b and reg.get(t) is not None, t
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", b)):
        assert env in src, env
    for word in ("fleet_nonces.json",):
        if word in b:
            assert word in src


def test_restart_states_match_tool_output(tmp_path):
    b = body("fleet-restart-safety")
    bare = S(enabled=True, _nonces={})
    assert "memory-only" in asyncio.run(gv.t_replay_guard(S(core=S(fleet=bare)), {})) and "memory-only" in b
    p = tmp_path / "fleet_nonces.json"
    p.write_text("{}")
    f = S(enabled=True, _nonces={}, _nonce_path=p, _boot_guard=time.time())
    out = asyncio.run(gv.t_replay_guard(S(core=S(fleet=f)), {}))
    assert "boot guard: active" in out and "boot guard: active" in b and "persisted=yes" in out


def test_stream_states_match(tmp_path):
    cx = Codex(tmp_path)
    q = asyncio.Queue(maxsize=1)
    cx._subscribers.add(q)
    cx.emit("r", "k", "a")
    out = asyncio.run(gv.t_stream_status(S(core=S(codex=cx)), {}))
    assert "FULL" in out and "FULL" in body("event-stream-health")
    assert "1000-event" in body("event-stream-health") and cx.subscribe().maxsize == 1000
