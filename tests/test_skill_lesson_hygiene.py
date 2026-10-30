"""lesson-hygiene skill: valid, grounded, and its full cycle works with the real tools:
learn -> recall -> retract (correction on the bridge) -> relearn the fix -> no open questions."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from avrone import bridge as bm
from avrone import evolve
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools

ROOT = Path(__file__).resolve().parent.parent
BODY = (ROOT / "skills" / "lesson-hygiene" / "SKILL.md").read_text()


def test_valid_and_grounded():
    assert evolve.check_skill("lesson-hygiene", BODY) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for t in ("lesson_recall", "lesson_forget", "research_note", "bridge_status", "bridge_reply",
              "read_file", "shell", "inspect_tool"):
        assert reg.get(t) is not None, t
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", BODY)):
        assert env in src, env


def test_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    reg = builtin_registry()
    register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)), codex=None)
    ctx = SimpleNamespace(core=core, run_id="h1", name="W-1", role="worker", depth=0)

    def call(name, **a):
        return asyncio.run(reg.call(ctx, name, a))

    wrong = "the queue needs a global lock for claims"
    call("research_note", text=wrong, kind="conclusion")
    q = bm.for_core(core, "h1").post("W-2", "question", "is the global lock still needed?")
    key = bm.lesson_key(wrong)
    assert key in call("lesson_recall", topic="queue lock claims")
    assert call("lesson_forget", key=key, reason="BEGIN IMMEDIATE replaced it").startswith("forgot")
    assert "NEWLY LEARNED" in call("research_note", text="claims use sqlite BEGIN IMMEDIATE, no global lock",
                                   kind="finding")
    assert "open questions (no reply yet): 1" in call("bridge_status")
    call("bridge_reply", refs=[q["id"]], kind="conclusion", text="no: BEGIN IMMEDIATE serialises claims")
    st = call("bridge_status")
    assert "open questions: none" in st and "correction=1" in st
    rec = call("lesson_recall", topic="queue claims lock")
    assert "BEGIN IMMEDIATE" in rec and key not in rec
