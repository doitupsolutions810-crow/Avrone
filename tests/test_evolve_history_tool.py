"""evolve_history as a registered agent tool, plus the evolve-loop-operations skill. Offline."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from avrone import agentkit, evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools
from tests.test_evolve_loop import BAD, GATE, GOOD, FakeRunner, env, mkctx  # noqa: F401

SKILL = Path(__file__).resolve().parent.parent / "skills" / "evolve-loop-operations" / "SKILL.md"


def run(c): return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AVRONE_EVOLVE_HISTORY", raising=False)
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)
    evolve._BG.clear()
    yield
    evolve._BG.clear()


def hist_file(env):
    return env.tmp / "d" / "evolve" / "loops" / "history.jsonl"


def seed(env, n):
    p = hist_file(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps({"loop_id": f"L{i}", "goal": f"goal {i}", "status": "done",
                                     "stopped": "max_iters", "iterations": 1, "max_iters": 3,
                                     "duration_s": 1.5}) + "\n" for i in range(n)))


def test_tool_is_registered_in_both_registries():
    names = {n for n, *_ in evolve.EVOLVE_TOOLS}
    assert "evolve_history" in names
    reg = builtin_registry()
    register_extra_tools(reg)
    assert "evolve_history" in set(reg.names())
    entry = next(e for e in evolve.EVOLVE_TOOLS if e[0] == "evolve_history")
    assert entry[3] is evolve.t_evolve_history and "limit" in entry[2]


def test_empty_history(env):
    out = run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), {}))
    assert out.startswith("no finished evolve loops yet")


def test_lists_real_loops_with_outcome(env):
    ctx = mkctx(env.tmp, FakeRunner())
    r = run(evolve.evolve_loop(ctx, "fix value", candidates=[BAD, GOOD], test_cmd=GATE))
    out = run(evolve.t_evolve_history(ctx, {}))
    assert r["loop_id"] in out and "[done/accepted]" in out and f"accepted={r['accepted']['pid']}" in out
    assert "iters=2/" in out and out.startswith("evolve history: 1 loop(s)")


def test_default_limit_is_ten_newest(env):
    seed(env, 30)
    out = run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), {}))
    ids = re.findall(r"^  (L\d+) ", out, re.M)
    assert ids == [f"L{i}" for i in range(20, 30)]


@pytest.mark.parametrize("limit,expect", [(1, 1), ("3", 3), (0, 1), (-5, 1), (10 ** 9, 50), (2.7, 2), ("", 10)])
def test_limit_is_bounded(env, limit, expect):
    seed(env, 60)  # file holds 60 rows; history_cap() keeps 50
    out = run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), {"limit": limit}))
    assert len(re.findall(r"^  L\d+ ", out, re.M)) == expect


@pytest.mark.parametrize("bad", ["abc", "nan", [1], {"a": 1}, True])
def test_bad_limit_is_refused(env, bad):
    seed(env, 3)
    out = run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), {"limit": bad}))
    assert out.startswith("ERROR") and str(evolve.HISTORY_CAP) in out


def test_env_lowered_history_cap_applies(env, monkeypatch):
    seed(env, 20)
    monkeypatch.setenv("AVRONE_EVOLVE_HISTORY", "4")
    out = run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), {"limit": 200}))
    assert re.findall(r"^  (L\d+) ", out, re.M) == ["L16", "L17", "L18", "L19"]


def test_denied(env, monkeypatch):
    seed(env, 2)
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    assert run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), {})) == evolve.DENIED_MSG


def test_non_dict_args_use_default(env):
    seed(env, 2)
    assert run(evolve.t_evolve_history(mkctx(env.tmp, FakeRunner()), None)).startswith("evolve history: 2")


# ---- the evolve-loop-operations skill ----

def test_skill_valid_and_names_real_tools():
    md = SKILL.read_text(encoding="utf-8")
    assert evolve.check_skill("evolve-loop-operations", md) is None
    fm, body = agentkit.parse_skill(md)
    assert fm["name"] == "evolve-loop-operations"
    reg = builtin_registry()
    register_extra_tools(reg)
    names = set(reg.names())
    ticks = {t for t in re.findall(r"`([a-z_]+)`", body)}
    for t in ("evolve_loop", "evolve_start", "evolve_status", "evolve_cancel", "evolve_history"):
        assert t in ticks
    assert ticks <= names


def test_skill_env_vars_exist_in_code():
    md = SKILL.read_text(encoding="utf-8")
    src = (Path(evolve.__file__)).read_text(encoding="utf-8")
    envs = set(re.findall(r"AVRONE_[A-Z_]+", md))
    assert {"AVRONE_SELF_EVOLVE", "AVRONE_EVOLVE_MAX_BG", "AVRONE_EVOLVE_HISTORY"} <= envs
    for e in envs:
        assert e in src, e
