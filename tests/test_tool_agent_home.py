"""agent_home: read-only report on the calling agent's OWN home (offline, no docker needed)."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from avrone import agentcontainer, agentpack
from avrone import tools_agenthome as ah
from avrone import tools_extra as tx
from avrone.tools import builtin_registry


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_CONTAINER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_NODE_ID", "node-a")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    ws, data = tmp_path / "ws", tmp_path / "data"
    ws.mkdir()
    data.mkdir()
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(workspace=str(ws), data_dir=str(data)),
                           runner=SimpleNamespace(kind="local"))
    ctx = SimpleNamespace(core=core, run_id="r1", name="Builder-1", depth=0, agent_id="builder")
    home = ws / "agents" / "builder"
    home.mkdir(parents=True)
    return SimpleNamespace(reg=reg, ctx=ctx, ws=ws, data=data, home=home, tmp=tmp_path)


def call(env, **args):
    return run(env.reg.call(env.ctx, "agent_home", args))


def js(env):
    return json.loads(call(env, format="json"))


def test_registered(env):
    t = env.reg.get("agent_home")
    assert t is not None and t.source == "builtin"
    assert env.reg.check("agent_home", {}) is None


def test_usage_quota_and_counts(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    (env.home / "a.txt").write_bytes(b"x" * 1000)
    (env.home / "sub").mkdir()
    (env.home / "sub" / "b.txt").write_bytes(b"y" * 24)
    r = js(env)
    assert r["agent_id"] == "builder" and r["path"] == "agents/builder" and r["exists"]
    assert r["bytes"] == 1024 and r["files"] == 2 and r["dirs"] == 1
    assert r["quota_bytes"] == 1024 * 1024 and not r["over_quota"] and r["free_bytes"] == 1024 * 1024 - 1024
    out = call(env)
    assert "agents/builder" in out and "2 files" in out and "OVER QUOTA" not in out
    assert str(env.ws) not in out and str(env.tmp) not in out   # path is workspace-relative only


def test_over_quota_flagged(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "0.001")   # ~1 KB
    (env.home / "big").write_bytes(b"z" * 5000)
    r = js(env)
    assert r["over_quota"] and r["free_bytes"] == 0
    assert "OVER QUOTA" in call(env)


def test_symlinks_never_followed(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "huge").write_bytes(b"q" * 100_000)
    os.symlink(outside, env.home / "escape_dir")
    os.symlink(outside / "huge", env.home / "escape_file")
    r = js(env)
    assert r["bytes"] == 0 and r["files"] == 0 and r["symlinks"] == 2
    assert "symlink" in call(env)


def test_symlinked_home_refused(env):
    real = env.tmp / "elsewhere"
    real.mkdir()
    (real / "f").write_bytes(b"1" * 77)
    env.home.rmdir()
    os.symlink(real, env.home)
    r = js(env)
    assert "error" in r and "bytes" not in r
    assert "ERROR" in call(env)


def test_missing_home_and_no_agent(env):
    env.home.rmdir()
    r = js(env)
    assert not r["exists"] and r["bytes"] == 0
    assert not env.home.exists()            # read-only: never creates the dir
    env.ctx.agent_id = None
    assert call(env).startswith("ERROR: you have no agent home")


def test_scan_is_bounded(env):
    for i in range(30):
        (env.home / f"f{i}").write_bytes(b"a")
    rep = ah.scan_home(env.home, limit=10)
    assert rep["truncated"] and rep["files"] == 10


def test_own_agent_only(env):
    other = env.ws / "agents" / "scout"
    other.mkdir()
    (other / "secret.txt").write_bytes(b"s" * 9999)
    agentpack.record(env.data, "scout", home="node-z", origin="node-z", created=5)
    pd = agentpack.pack_dir(env.data)
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "scout-99.tar.gz").write_bytes(b"x")
    (pd / "builder-x-7.tar.gz").write_bytes(b"x")   # another agent whose id starts with ours
    out = call(env, agent="scout", agent_id="scout", path="../scout")   # args cannot pick an agent
    assert "scout" not in out and "node-z" not in out and "9999" not in out
    r = js(env)
    assert r["pack"]["count"] == 0 and r["pack"]["record"] is None


def test_pack_record_home_node_and_newest_pack(env):
    agentpack.record(env.data, "builder", home="node-b", origin="node-b", created=100, imported=200)
    agentpack.record(env.data, "builder", migrated_to="node-c", migrated_stamp=300, migrated_at=400,
                     junk="\x00evil" * 50)
    pd = agentpack.pack_dir(env.data)
    for n in ("builder-100.tar.gz", "builder-300.tar.gz"):
        (pd / n).write_bytes(b"x" * 10)
    os.symlink(pd / "builder-300.tar.gz", pd / "builder-999.tar.gz")   # a symlinked pack is ignored
    r = js(env)
    p = r["pack"]
    assert p["node"] == "node-a" and p["home_node"] == "node-b" and not p["born_here"]
    assert p["record"]["migrated_to"] == "node-c" and "junk" not in p["record"]
    assert p["count"] == 2 and p["newest"]["name"] == "builder-300.tar.gz"
    out = call(env)
    assert "home node: node-b" in out and "builder-300.tar.gz" in out and "migrated_to=node-c" in out


def test_real_export_shows_up(env):
    (env.home / "notes.md").write_text("hello")
    run(env.reg.call(env.ctx, "agent_pack_export", {}))
    r = js(env)
    assert r["pack"]["count"] == 1 and r["pack"]["newest"]["name"].startswith("builder-")
    assert r["pack"]["born_here"]


def test_container_state_without_docker(env, monkeypatch):
    monkeypatch.setattr(ah.shutil, "which", lambda name: None)
    c = js(env)["container"]
    assert c["mode"] == "off" and not c["docker_present"] and not c["hosted"] and c["live"] is None
    assert c["name"] == agentcontainer.container_name("node-a", "builder")
    assert "docker=no" in call(env)


def test_container_hosted_live_record_own_only(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    monkeypatch.setattr(ah.shutil, "which", lambda name: "/usr/bin/docker")

    async def fake_exec(argv, timeout):
        return SimpleNamespace(exit_code=0, stdout="true", stderr="")
    t = [100.0]
    pool = agentcontainer.AgentContainerPool(fake_exec, "img", False, node="node-a", clock=lambda: t[0])
    run(pool.ensure(env.home))
    run(pool.ensure(env.ws / "agents" / "scout"))
    t[0] = 112.5
    env.ctx.core.runner = SimpleNamespace(kind="docker", agent_pool=pool)
    c = js(env)["container"]
    assert c["mode"] == "persistent" and c["hosted"] and c["docker_present"]
    assert c["name"] == "avrone-agent-node-a-builder" and c["live"] == {"uses": 1, "idle_s": 12.5}
    out = call(env)
    assert "hosted=yes" in out and "scout" not in out


def test_output_capped(env, monkeypatch):
    monkeypatch.setattr(ah, "OUT_MAX_CHARS", 50)
    assert len(call(env)) <= 50 + len("\n...[truncated]")
