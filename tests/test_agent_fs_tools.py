"""fs_write / fs_read / fs_list / fs_delete: confinement, caps, quota, audit."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from avrone import agentfs
from avrone import agentfs_tools as aft
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry
from avrone.tools_extra import register_extra_tools

MB = 1024 * 1024


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_FS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    runner = LocalRunner(s)
    run(runner.start())
    codex = Codex(s.data_dir)
    core = SimpleNamespace(settings=s, runner=runner, codex=codex)
    d = agentfs.agent_dir(s.workspace, "alpha")
    ctx = SimpleNamespace(core=core, run_id="run1", name="Alpha-1", depth=0, agent_id="alpha", agent_dir=d)
    reg = ToolRegistry()
    aft.register_agent_fs_tools(reg)
    return SimpleNamespace(s=s, ws=s.workspace, codex=codex, ctx=ctx, reg=reg, dir=d, tmp=tmp_path)


def call(env, tool, ctx=None, **args):
    return run(env.reg.call(ctx or env.ctx, tool, args))


def events(env, kind):
    return [e for e in env.codex.replay("run1") if e["kind"] == kind]


# ------------------------------------------------------------------ basics
def test_registered_in_extra_tools():
    reg = ToolRegistry()
    register_extra_tools(reg)
    assert {"fs_write", "fs_read", "fs_list", "fs_delete"} <= set(reg.names())


def test_write_read_list_delete_roundtrip(env):
    out = call(env, "fs_write", path="notes/a.txt", content="hello")
    assert out.startswith("wrote notes/a.txt (5 bytes")
    assert (env.dir / "notes" / "a.txt").read_text() == "hello"
    assert call(env, "fs_read", path="notes/a.txt") == "hello"
    call(env, "fs_write", path="notes/a.txt", content=" world", append=True)
    assert call(env, "fs_read", path="notes/a.txt") == "hello world"
    listing = call(env, "fs_list", recursive=True)
    assert "notes/" in listing and "notes/a.txt  11 bytes" in listing
    assert "11/" in listing.splitlines()[0]            # usage/quota header
    assert call(env, "fs_list", path="notes") .count("a.txt") == 1
    assert "deleted notes/a.txt (11 bytes freed)" == call(env, "fs_delete", path="notes/a.txt")
    assert not (env.dir / "notes" / "a.txt").exists()
    assert "(empty)" in call(env, "fs_list", path="notes")


def test_files_live_only_in_agent_dir(env):
    call(env, "fs_write", path="x.txt", content="1")
    assert (env.ws / "agents" / "alpha" / "x.txt").exists()
    assert not (env.ws / "x.txt").exists()


def test_persistence_across_contexts_same_agent(env):
    call(env, "fs_write", path="memo.md", content="remember me")
    ctx2 = SimpleNamespace(**{**vars(env.ctx), "run_id": "run2", "name": "Alpha-7",
                              "agent_dir": agentfs.agent_dir(env.ws, "alpha")})
    assert call(env, "fs_read", ctx=ctx2, path="memo.md") == "remember me"


def test_agents_cannot_see_each_other(env):
    call(env, "fs_write", path="secret.txt", content="alpha only")
    bctx = SimpleNamespace(**{**vars(env.ctx), "agent_id": "beta", "name": "Beta-1",
                              "agent_dir": agentfs.agent_dir(env.ws, "beta")})
    assert "not found" in call(env, "fs_read", ctx=bctx, path="secret.txt")
    assert "ERROR" in call(env, "fs_read", ctx=bctx, path="../alpha/secret.txt")


def test_read_missing_and_dir(env):
    assert "not found" in call(env, "fs_read", path="nope.txt")
    (env.dir / "sub").mkdir()
    assert "directory" in call(env, "fs_read", path="sub")


def test_read_truncates(env):
    call(env, "fs_write", path="big.txt", content="a" * 5000)
    out = call(env, "fs_read", path="big.txt", max_chars=100)
    assert out.startswith("a" * 100) and "truncated" in out and "5000 bytes" in out


def test_no_agent_dir_is_an_error(env):
    ctx = SimpleNamespace(core=env.ctx.core, run_id="run1", name="X", depth=0)  # legacy ctx
    assert "no agent dir" in call(env, "fs_write", ctx=ctx, path="a", content="b")
    assert "no agent dir" in call(env, "fs_list", ctx=ctx)


# ------------------------------------------------------------------ confinement
EVIL = ["/etc/passwd", "../escape.txt", "a/../../escape.txt", "..", "~/x", "C:\\x",
        "..\\..\\x", "sub/../../../etc", "\\\\server\\share"]


@pytest.mark.parametrize("tool", ["fs_write", "fs_read", "fs_list", "fs_delete"])
@pytest.mark.parametrize("path", EVIL)
def test_rejects_absolute_and_traversal(env, tool, path):
    out = call(env, tool, path=path, content="pwn")
    assert out.startswith("ERROR"), out
    assert not (env.ws / "agents" / "escape.txt").exists()
    assert not (env.ws / "escape.txt").exists()
    assert events(env, "agentfs.denied"), "refusal must be audited"


def test_symlink_escape_blocked(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("host secret")
    os.symlink(outside, env.dir / "link")
    os.symlink(outside / "target.txt", env.dir / "flink")
    assert call(env, "fs_read", path="link/target.txt").startswith("ERROR")
    assert call(env, "fs_read", path="flink").startswith("ERROR")
    assert call(env, "fs_write", path="link/new.txt", content="x").startswith("ERROR")
    assert call(env, "fs_write", path="flink", content="x").startswith("ERROR")
    assert call(env, "fs_list", path="link").startswith("ERROR")
    assert call(env, "fs_delete", path="link/target.txt").startswith("ERROR")
    assert not (outside / "new.txt").exists()
    assert (outside / "target.txt").read_text() == "host secret"
    # deleting the symlink itself removes the link, never the outside target
    assert call(env, "fs_delete", path="flink").startswith("deleted flink")
    assert call(env, "fs_delete", path="link").startswith("deleted link")
    assert not os.path.lexists(env.dir / "link") and (outside / "target.txt").exists()


def test_cannot_delete_agent_dir_itself(env):
    for p in ("", ".", "./"):
        assert call(env, "fs_delete", path=p).startswith("ERROR")
    assert env.dir.is_dir()


def test_delete_dir_requires_recursive(env):
    call(env, "fs_write", path="d/one.txt", content="1")
    assert "recursive" in call(env, "fs_delete", path="d")
    assert call(env, "fs_delete", path="d", recursive=True).startswith("deleted d")
    assert not (env.dir / "d").exists()
    assert "not found" in call(env, "fs_delete", path="d")


# ------------------------------------------------------------------ caps / quota
def test_defaults_and_hard_caps(monkeypatch):
    monkeypatch.delenv("AVRONE_AGENT_FS_QUOTA_MB", raising=False)
    monkeypatch.delenv("AVRONE_AGENT_FS_MAX_FILE_MB", raising=False)
    assert aft.quota_bytes() == 50 * MB and aft.max_file_bytes() == 5 * MB
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "999999")
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "999999")
    assert aft.quota_bytes() == aft.HARD_QUOTA_MB * MB
    assert aft.max_file_bytes() == aft.HARD_FILE_MB * MB
    for junk in ("abc", "-5", "0", ""):
        monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", junk)
        assert aft.quota_bytes() == 50 * MB
    # the per-file cap never exceeds the quota
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "10")
    assert aft.max_file_bytes() == 1 * MB


def test_per_file_cap_enforced_and_audited(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "0.001")  # 1048 bytes
    cap = aft.max_file_bytes()
    assert call(env, "fs_write", path="ok.txt", content="x" * cap).startswith("wrote")
    out = call(env, "fs_write", path="big.txt", content="x" * (cap + 1))
    assert out.startswith("ERROR") and "per-file cap" in out
    assert not (env.dir / "big.txt").exists()
    out = call(env, "fs_write", path="ok.txt", content="y", append=True)  # append over cap
    assert "per-file cap" in out
    denied = events(env, "agentfs.denied")
    assert denied[-1]["data"]["reason"] == "file_cap" and denied[-1]["data"]["agent_id"] == "alpha"


def test_quota_enforced_and_audited(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "0.002")       # ~2097 bytes
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "1")        # clamped to quota
    q = aft.quota_bytes()
    assert call(env, "fs_write", path="a.bin", content="a" * 1000).startswith("wrote")
    assert call(env, "fs_write", path="b.bin", content="b" * 1000).startswith("wrote")
    out = call(env, "fs_write", path="c.bin", content="c" * 1000)
    assert out.startswith("ERROR") and "quota" in out
    assert not (env.dir / "c.bin").exists()
    ev = events(env, "agentfs.denied")[-1]["data"]
    assert ev["reason"] == "quota" and ev["quota"] == q and ev["usage"] == 2000
    # overwriting an existing file counts only the difference
    assert call(env, "fs_write", path="a.bin", content="a" * 1050).startswith("wrote")
    # freeing space lets the write through
    call(env, "fs_delete", path="b.bin")
    assert call(env, "fs_write", path="c.bin", content="c" * 1000).startswith("wrote")
    assert aft.dir_usage(env.dir) <= q


def test_quota_counts_files_made_outside_the_tools(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "0.001")
    (env.dir / "from_shell.bin").write_bytes(b"z" * 1000)   # e.g. created via the shell tool
    assert "quota" in call(env, "fs_write", path="n.txt", content="x" * 100)


def test_successful_ops_are_audited(env):
    call(env, "fs_write", path="a.txt", content="abc")
    call(env, "fs_delete", path="a.txt")
    w = events(env, "agentfs.write")[-1]
    d = events(env, "agentfs.delete")[-1]
    assert w["agent"] == "Alpha-1" and w["data"]["path"] == "a.txt" and w["data"]["bytes"] == 3
    assert w["data"]["agent_id"] == "alpha" and "quota" in w["data"]
    assert d["data"]["path"] == "a.txt" and d["data"]["bytes"] == 3


def test_write_to_directory_rejected(env):
    (env.dir / "sub").mkdir()
    assert call(env, "fs_write", path="sub", content="x").startswith("ERROR")
    assert call(env, "fs_write", path=".", content="x").startswith("ERROR")
