import asyncio
import time
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import tools_extra as tx
from avrone import tools_lessonrecall as lr
from avrone.tools import builtin_registry

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path)))
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, core=core, tmp=tmp_path)


def call(env, **args):
    return asyncio.run(env.reg.call(env.ctx, "lesson_recall", args))


def save(env, text, task="", ts=None):
    st = bm._store(env.tmp)
    rec = st.save(bm.LESSONS_NS, bm.lesson_key(text), text, task=task)
    return rec


def test_registered(env):
    assert env.reg.get("lesson_recall").source == "builtin"
    assert env.reg.check("lesson_recall", {}) is None


def test_empty_and_off_and_no_data_dir(env, monkeypatch):
    assert "no lessons stored yet" in call(env)
    env.core.settings = SimpleNamespace(data_dir=None)
    assert "no data_dir" in call(env)
    monkeypatch.setenv("AVRONE_LESSONS", "off")
    assert "lessons are off" in call(env)


def test_topic_ranking_and_newest_listing(env):
    save(env, "caching makes builds faster", task="build")
    time.sleep(0.01)
    save(env, "caching and retries make deploys faster", task="deploy")
    time.sleep(0.01)
    save(env, "unrelated note on fonts")
    out = call(env, topic="caching retries")
    lines = out.splitlines()
    assert "2 of 2 matching" in lines[0]
    assert "retries" in lines[1] and "(deploy)" in lines[1]
    newest = call(env)
    assert "fonts" in newest.splitlines()[1] and "newest first" in newest
    assert "share words" in call(env, topic="quantum entanglement")


def test_limit_clamp_and_bad(env):
    for i in range(60):
        save(env, f"lesson {i} about widgets")
    out = call(env, limit=500)
    assert "50 of 60" in out
    assert "1 of 60" in call(env, limit=0)
    assert "1 of 60" in call(env, limit="1")
    assert "ERROR" in call(env, limit=True)
    assert "ERROR" in call(env, limit="x")


def test_redaction_digest_and_cap(env, monkeypatch):
    save(env, f"token {SECRET} leaked " + "w" * 400, task=f"Bearer {SECRET}")
    out = call(env)
    assert SECRET not in out and "..." in out
    monkeypatch.setattr(lr, "LESSON_RECALL_MAX_CHARS", 200)
    for i in range(5):
        save(env, f"another lesson {i} " + "q" * 100)
    assert "more not shown" in call(env)


def test_closes_the_loop_with_research_note(env):
    asyncio.run(env.reg.call(env.ctx, "research_note", {"text": "Use BEGIN IMMEDIATE for sqlite claims"}))
    assert "BEGIN IMMEDIATE" in call(env, topic="sqlite claims")


def test_age_format():
    assert lr.age(5) == "5s" and lr.age(120) == "2m" and lr.age(7200) == "2h" and lr.age(172800) == "2d"
    assert lr.age(-3) == "0s"


def test_rank_bad_ts():
    recs = [{"value": "alpha beta", "ts": "bad"}, {"value": "alpha", "ts": 5}]
    ranked, by = lr.rank(recs, "alpha beta")
    assert by and ranked[0]["value"] == "alpha beta"
    assert lr.rank(recs, "")[1] is False
