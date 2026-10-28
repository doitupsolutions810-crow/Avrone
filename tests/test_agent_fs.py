"""Per-agent persistent workspaces: derivation, confinement, persistence, runner cwd, docker argv."""
import asyncio
import os
from pathlib import Path

import pytest

from avrone import agentfs
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import DockerRunner, LocalRunner
from avrone.trinity import TrinityCore


def run(coro):
    return asyncio.run(coro)


class ScriptLLM:
    """Replays tool calls, then finishes. Records nothing else."""
    def __init__(self, replies):
        self.replies = list(replies)

    async def chat(self, messages, **kw):
        return self.replies.pop(0) if self.replies else '{"final": "done"}'


def _settings(tmp_path, **kw):
    s = Settings()
    s.workspace = tmp_path / "ws"
    s.data_dir = tmp_path / "data"
    s.max_steps = 4
    s.agents_can_shell = True
    s.command_timeout = 30
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _core(tmp_path, replies, **kw):
    s = _settings(tmp_path, **kw)
    runner = LocalRunner(s)
    run(runner.start())
    return TrinityCore(s, ScriptLLM(replies), runner, Codex(s.data_dir)), s


# ------------------------------------------------------------------ sanitize / derive
@pytest.mark.parametrize("raw,expected", [
    ("Researcher", "researcher"),
    ("Fact-Checker", "fact-checker"),
    ("../../etc/passwd", "etc-passwd"),
    ("/abs/path", "abs-path"),
    ("..", "agent"),
    ("", "agent"),
    (None, "agent"),
    ("a b\\c/d", "a-b-c-d"),
    ("x" * 200, "x" * agentfs.MAX_ID_LEN),
    ("__hidden__", "hidden"),
    (".bashrc", "bashrc"),
])
def test_sanitize_agent_id(raw, expected):
    out = agentfs.sanitize_agent_id(raw)
    assert out == expected
    assert "/" not in out and "\\" not in out and ".." not in out and out not in {".", ""}


def test_agent_dir_created_confined_and_private(tmp_path):
    ws = tmp_path / "ws"
    d = agentfs.agent_dir(ws, "Researcher")
    assert d.is_dir()
    assert d == (ws / "agents" / "researcher").resolve()
    assert d.parent == agentfs.agents_root(ws)
    assert d.is_relative_to(ws.resolve())
    assert (d.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("evil", ["../../outside", "/etc", "..", "a/../../b", "..\\..\\x", "~root"])
def test_agent_dir_cannot_escape(tmp_path, evil):
    ws = tmp_path / "ws"
    d = agentfs.agent_dir(ws, evil)
    assert d.parent == agentfs.agents_root(ws)
    assert not (tmp_path / "outside").exists()


def test_agent_dir_rejects_symlink_escape(tmp_path):
    ws = tmp_path / "ws"
    (ws / "agents").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws / "agents" / "evil")
    with pytest.raises(agentfs.AgentFSError):
        agentfs.agent_dir(ws, "evil")


def test_agents_root_symlink_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws / "agents")
    with pytest.raises(agentfs.AgentFSError):
        agentfs.agent_dir(ws, "a")


def test_create_false_does_not_make_dirs(tmp_path):
    ws = tmp_path / "ws"
    d = agentfs.agent_dir(ws, "lazy", create=False)
    assert not d.exists() and d.parent == agentfs.agents_root(ws)


def test_is_agent_dir(tmp_path):
    ws = tmp_path / "ws"
    d = agentfs.agent_dir(ws, "a1")
    assert agentfs.is_agent_dir(ws, d)
    assert not agentfs.is_agent_dir(ws, ws)
    assert not agentfs.is_agent_dir(ws, ws / "agents")
    assert not agentfs.is_agent_dir(ws, d / "sub")
    assert not agentfs.is_agent_dir(ws, tmp_path)
    assert not agentfs.is_agent_dir(ws, None)


# ------------------------------------------------------------------ AgentContext + persistence
def _capture_ctx(core):
    seen = []
    orig = core.tools.call

    async def spy(ctx, name, args):
        seen.append(ctx)
        return await orig(ctx, name, args)
    core.tools.call = spy
    return seen


def test_agent_dir_stable_across_runs_and_files_persist(tmp_path):
    write = '{"tool": "shell", "args": {"cmd": "echo kept > note.txt && pwd"}}'
    read = '{"tool": "shell", "args": {"cmd": "cat note.txt"}}'
    core, s = _core(tmp_path, [write, '{"final": "w"}', read, '{"final": "r"}'])
    seen = _capture_ctx(core)
    run(core.run_agent("run-a", "Researcher", "task one"))
    run(core.run_agent("run-b", "Researcher", "task two"))
    a, b = seen[0], seen[1]
    assert a.name != b.name                       # different agent instances...
    assert a.agent_id == b.agent_id == "researcher"
    assert a.agent_dir == b.agent_dir == (s.workspace / "agents" / "researcher").resolve()
    assert (a.agent_dir / "note.txt").read_text().strip() == "kept"
    # the second run read the file the first run left behind
    results = [e for e in core.codex.replay("run-b") if e["kind"] == "tool.result"]
    assert "kept" in results[0]["data"]["output"]
    # not written at the shared workspace root
    assert not (s.workspace / "note.txt").exists()


def test_distinct_dirs_per_agent(tmp_path):
    cmd = '{"tool": "shell", "args": {"cmd": "pwd"}}'
    core, s = _core(tmp_path, [cmd, '{"final": "1"}', cmd, '{"final": "2"}'])
    seen = _capture_ctx(core)
    run(core.run_agent("r", "Researcher", "t"))
    run(core.run_agent("r", "Coder", "t"))
    assert seen[0].agent_dir != seen[1].agent_dir
    assert {seen[0].agent_dir.name, seen[1].agent_dir.name} == {"researcher", "coder"}


def test_explicit_agent_id_overrides_role(tmp_path):
    core, s = _core(tmp_path, ['{"tool": "shell", "args": {"cmd": "true"}}'])
    seen = _capture_ctx(core)
    run(core.run_agent("r", "Whatever", "t", agent_id="../My Bot"))
    assert seen[0].agent_id == "my-bot"
    assert seen[0].agent_dir == (s.workspace / "agents" / "my-bot").resolve()


def test_agent_fs_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS", "0")
    core, s = _core(tmp_path, ['{"tool": "shell", "args": {"cmd": "pwd"}}'])
    seen = _capture_ctx(core)
    run(core.run_agent("r", "Researcher", "t"))
    assert seen[0].agent_dir is None and seen[0].agent_id is None
    assert not (s.workspace / "agents").exists()


# ------------------------------------------------------------------ LocalRunner cwd
def test_local_runner_agent_cwd_and_env(tmp_path):
    s = _settings(tmp_path)
    r = LocalRunner(s)
    run(r.start())
    d = agentfs.agent_dir(s.workspace, "alpha")
    res = run(r.run('pwd; echo "$AVRONE_AGENT_DIR"; echo "$AVRONE_SHARED_WORKSPACE"', cwd=d))
    lines = res.stdout.split()
    assert Path(lines[0]).resolve() == d
    assert Path(lines[1]).resolve() == d
    assert Path(lines[2]).resolve() == s.workspace.resolve()


def test_local_runner_non_agent_cwd_unchanged(tmp_path):
    s = _settings(tmp_path)
    r = LocalRunner(s)
    run(r.start())
    res = run(r.run("pwd"))
    assert Path(res.stdout.strip()).resolve() == s.workspace.resolve()


@pytest.mark.parametrize("bad", ["root", "agents", "outside", "nested"])
def test_runner_rejects_unconfined_cwd(tmp_path, bad):
    s = _settings(tmp_path)
    r = LocalRunner(s)
    run(r.start())
    d = agentfs.agent_dir(s.workspace, "alpha")
    (d / "sub").mkdir()
    target = {"root": s.workspace, "agents": s.workspace / "agents", "outside": tmp_path,
              "nested": d / "sub"}[bad]
    with pytest.raises(agentfs.AgentFSError):
        run(r.run("pwd", cwd=target))


# ------------------------------------------------------------------ DockerRunner argv (offline)
def test_docker_exec_argv_shared_container(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_AGENT_DOCKER_ISOLATE", raising=False)
    s = _settings(tmp_path)
    r = DockerRunner(s)
    d = agentfs.agent_dir(s.workspace, "Researcher")
    argv = r.exec_argv("ls", r._agent_cwd(d))
    assert argv[:2] == ["docker", "exec"]
    assert argv[argv.index("-w") + 1] == "/workspace/agents/researcher"
    assert "AVRONE_AGENT_DIR=/workspace/agents/researcher" in argv
    assert "AVRONE_SHARED_WORKSPACE=/workspace" in argv
    assert argv[-4:] == [r.container, "bash", "-lc", "ls"]
    # non-agent command is unchanged
    assert r.exec_argv("ls") == ["docker", "exec", r.container, "bash", "-lc", "ls"]


def test_docker_isolated_mounts_agent_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", "1")
    s = _settings(tmp_path, allow_network=False, docker_image="python:3.12-slim")
    r = DockerRunner(s)
    d = agentfs.agent_dir(s.workspace, "Coder")
    argv = r.exec_argv("make", r._agent_cwd(d))
    assert argv[:3] == ["docker", "run", "--rm"]
    vi = argv.index("-v")
    assert argv[vi + 1] == f"{d}:/agent"
    assert argv.count("-v") == 1                      # ONLY the agent dir is mounted
    assert argv[argv.index("-w") + 1] == "/agent"
    assert "AVRONE_AGENT_DIR=/agent" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[-4:] == ["python:3.12-slim", "bash", "-lc", "make"]


def test_agent_volume_args(tmp_path):
    d = agentfs.agent_dir(tmp_path / "ws", "z")
    assert agentfs.agent_volume_args(d) == ["-v", f"{d}:/agent"]


def test_docker_run_passes_argv_without_docker(tmp_path, monkeypatch):
    """run() builds the argv and hands it to _exec; docker itself is never invoked."""
    monkeypatch.delenv("AVRONE_AGENT_DOCKER_ISOLATE", raising=False)
    s = _settings(tmp_path)
    r = DockerRunner(s)
    got = {}

    async def fake_exec(argv, cmd, timeout, cwd=None, env=None):
        got["argv"] = argv
        from avrone.sovereign import CommandResult
        return CommandResult(cmd, 0, "", "")
    monkeypatch.setattr(r, "_exec", fake_exec)
    d = agentfs.agent_dir(s.workspace, "w")
    run(r.run("echo hi", cwd=d))
    assert "/workspace/agents/w" in got["argv"]
    with pytest.raises(agentfs.AgentFSError):
        run(r.run("echo hi", cwd=tmp_path))
