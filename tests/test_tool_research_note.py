import asyncio
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import tools_extra as tx
from avrone import tools_researchnote as rn
from avrone.tools import builtin_registry

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", role="worker", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, core=core, tmp=tmp_path)


def call(env, name="research_note", **args):
    return asyncio.run(env.reg.call(env.ctx, name, args))


def lessons(env):
    return bm._store(env.tmp).load(bm.LESSONS_NS)


def test_registered_with_schema(env):
    assert env.reg.get("research_note").source == "builtin"
    assert env.reg.check("research_note", {})[0] == "bad_args"
    assert env.reg.check("research_note", {"text": "x"}) is None


def test_finding_posts_and_learns_then_known(env):
    out = call(env, text="Cache  warmup   halves cold start", topic="perf")
    assert "posted finding" in out and "NEWLY LEARNED" in out
    posts = bm.for_core(env.core, "run1").posts()
    assert posts[-1]["text"] == "[perf] Cache  warmup   halves cold start"
    out2 = call(env, text="Cache warmup halves\ncold start", kind="conclusion")
    assert "already known" in out2
    assert len(lessons(env)) == 1
    rec = next(iter(lessons(env).values()))
    assert rec["source"] == "research_note" and rec["task"] == "perf"


def test_visible_through_bridge_read(env):
    call(env, text="sibling should see this")
    assert "sibling should see this" in call(env, "bridge_read")


def test_question_posted_not_learned(env):
    out = call(env, text="why is the queue slow?", kind="question")
    assert "posted question" in out and "not stored" in out
    assert lessons(env) == {}


def test_lessons_off_posts_only(env, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "off")
    out = call(env, text="fact")
    assert "lessons: off" in out and "posted finding" in out


def test_no_data_dir(env):
    env.core.settings = SimpleNamespace(data_dir=None)
    assert "no data_dir" in call(env, text="fact")


@pytest.mark.parametrize("args,msg", [
    ({"text": ""}, "text is required"),
    ({"text": "x" * 4001}, "cap is 4000"),
    ({"text": "x", "kind": "rumor"}, "kind must be"),
    ({"text": "x", "topic": "t" * 201}, "topic is longer"),
])
def test_refusals(env, args, msg):
    assert msg in call(env, **args)
    assert bm.for_core(env.core, "run1").posts() == []


def test_secrets_masked_in_post_and_lesson(env):
    out = call(env, text=f"key was {SECRET} in env", topic=f"Bearer {SECRET}")
    assert "masked" in out and SECRET not in out
    assert SECRET not in str(bm.for_core(env.core, "run1").posts())
    assert SECRET not in str(lessons(env))


def test_same_key_as_distill_no_duplicate(env):
    call(env, text="Pin the httpx version", kind="conclusion")
    b = bm.for_core(env.core, "run1")
    assert bm.distill_lessons(env.tmp, b, "t") == []
    assert len(lessons(env)) == 1


def test_memory_refused(env, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_VALUE", "16")
    out = call(env, text="this lesson is definitely longer than sixteen bytes")
    assert "not stored (refused" in out


def test_long_topic_body_keeps_full_note(env):
    text = "y" * 3990
    out = call(env, text=text, topic="z" * 50)
    assert "posted finding" in out
    assert bm.for_core(env.core, "run1").posts()[-1]["text"] == text


def test_redact_text_variants():
    assert rn.redact_text("https://user:pw@host/x") == "https://***@host/x"
    assert "AKIA" not in rn.redact_text("AKIAABCDEFGHIJKLMNOP")
    assert rn.redact_text("aB3" * 12) == rn.REDACTED
    assert rn.redact_text("a" * 40) == "a" * 40
