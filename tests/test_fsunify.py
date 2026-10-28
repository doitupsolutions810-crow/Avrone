"""Generic file tools unified with the per-agent filesystem (fsunify lane)."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from avrone import agentfs, fsunify
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry, builtin_registry
from avrone.tools_extra import register_extra_tools

MB = 1024 * 1024


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_FS",
              "AVRONE_AGENT_DOCKER_ISOLATE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    runner = LocalRunner(s)
    run(runner.start())
    codex = Codex(s.data_dir)
    core = SimpleNamespace(settings=s, runner=runner, codex=codex)
    d = agentfs.agent_dir(s.workspace, "alpha")
    ctx = SimpleNamespace(core=core, run_id="run1", name="Alpha-1", depth=0, agent_id="alpha", agent_dir=d)
    legacy = SimpleNamespace(core=core, run_id="run1", name="Legacy-1", depth=0, agent_id=None, agent_dir=None)
    reg = builtin_registry()
    register_extra_tools(reg)
    return SimpleNamespace(s=s, ws=s.workspace.resolve(), codex=codex, ctx=ctx, legacy=legacy,
                           reg=reg, dir=d, tmp=tmp_path)


def call(env, tool, ctx=None, **args):
    return run(env.reg.call(ctx or env.ctx, tool, args))


def events(env, kind):
    return [e for e in env.codex.replay("run1") if e["kind"] == kind]


# ------------------------------------------------------------------ confinement to own dir
def test_write_read_land_in_agent_dir(env):
    out = call(env, "write_file", path="notes/a.txt", content="hello")
    assert out.startswith("wrote notes/a.txt (5 bytes; agent dir 5/")
    assert (env.dir / "notes" / "a.txt").read_text() == "hello"
    assert not (env.ws / "notes").exists()
    assert call(env, "read_file", path="notes/a.txt") == "hello"
    assert "not found" in call(env, "read_file", path="nope.txt")
    assert events(env, "agentfs.write")[-1]["data"]["tool"] == "write_file"


def test_fs_tools_and_generic_tools_share_the_dir(env):
    call(env, "fs_write", path="memo.md", content="from fs_write")
    assert call(env, "read_file", path="memo.md") == "from fs_write"
    call(env, "write_file", path="memo2.md", content="from write_file")
    assert call(env, "fs_read", path="memo2.md") == "from write_file"


def test_edit_file_in_agent_dir(env):
    call(env, "write_file", path="e.txt", content="one two three")
    assert call(env, "edit_file", path="e.txt", old="two", new="2") == "edited e.txt (1 replacement)"
    assert (env.dir / "e.txt").read_text() == "one 2 three"
    assert "not found in e.txt" in call(env, "edit_file", path="e.txt", old="zzz", new="y")
    call(env, "write_file", path="e.txt", content="x x")
    assert "matches 2 times in e.txt" in call(env, "edit_file", path="e.txt", old="x", new="y")
    assert "not found" in call(env, "edit_file", path="missing.txt", old="x", new="y")


def test_list_and_search_agent_dir(env):
    call(env, "write_file", path="src/m.py", content="needle = 1\n")
    (env.ws / "top.txt").write_text("needle in shared\n")
    listing = call(env, "list_files")
    assert listing.splitlines()[0] == "./"
    assert "src/" in listing and "m.py" in listing and "top.txt" not in listing
    assert call(env, "list_files", path="src/m.py").startswith("src/m.py (")
    hits = call(env, "search_files", pattern="needle")
    assert hits == "src/m.py:1: needle = 1"
    assert "not found" in call(env, "list_files", path="nope")
    assert "not found" in call(env, "search_files", pattern="x", path="nope")


def test_diff_files_in_agent_dir_and_across_roots(env):
    call(env, "write_file", path="a.txt", content="1\n2\n")
    (env.ws / "b.txt").write_text("1\n3\n")
    out = call(env, "diff_files", path_a="a.txt", path_b="shared/b.txt")
    assert "--- a.txt" in out and "+++ shared/b.txt" in out and "[1 added, 1 removed]" in out


# ------------------------------------------------------------------ shared/ prefix
def test_shared_prefix_reaches_old_workspace(env):
    out = call(env, "write_file", path="shared/team/plan.md", content="plan")
    assert out == "wrote shared/team/plan.md (4 bytes)"
    assert (env.ws / "team" / "plan.md").read_text() == "plan"
    assert call(env, "read_file", path="shared/team/plan.md") == "plan"
    assert call(env, "read_file", path="shared\\team\\plan.md") == "plan"
    assert call(env, "edit_file", path="shared/team/plan.md", old="plan", new="PLAN") == \
        "edited shared/team/plan.md (1 replacement)"
    assert (env.ws / "team" / "plan.md").read_text() == "PLAN"
    assert call(env, "search_files", pattern="PLAN", path="shared/") == "shared/team/plan.md:1: PLAN"


def test_shared_root_listing_hides_agents(env):
    call(env, "write_file", path="mine.txt", content="m")
    (env.ws / "pub.txt").write_text("needle")
    bdir = agentfs.agent_dir(env.ws, "beta")
    (bdir / "secret.txt").write_text("needle")
    listing = call(env, "list_files", path="shared")
    assert listing.splitlines()[0] == "shared/"
    assert "pub.txt" in listing and "agents" not in listing and "secret" not in listing
    assert call(env, "search_files", pattern="needle", path="shared") == "shared/pub.txt:1: needle"


def test_shared_agents_subtree_refused(env):
    bdir = agentfs.agent_dir(env.ws, "beta")
    (bdir / "secret.txt").write_text("beta only")
    for path in ("shared/agents", "shared/agents/beta/secret.txt", "shared/agents/alpha/x.txt"):
        assert call(env, "read_file", path=path).startswith("ERROR"), path
        assert call(env, "write_file", path=path, content="x").startswith("ERROR"), path
    assert not (env.dir / "x.txt").exists()
    assert events(env, "agentfs.denied")


def test_shared_only_exact_prefix(env):
    # "sharedfoo/x" and "shared_x" are ordinary paths in the agent's own dir
    call(env, "write_file", path="sharedfoo/x", content="1")
    assert (env.dir / "sharedfoo" / "x").exists()
    assert fsunify.split_shared("sharedfoo") is None
    assert fsunify.split_shared("shared") == ""
    assert fsunify.split_shared("shared//a") == "/a"
    assert fsunify.split_shared("shared/") == ""


# ------------------------------------------------------------------ escapes
EVIL = ["/etc/passwd", "../escape.txt", "a/../../escape.txt", "..", "~/x", "C:\\x",
        "shared/../escape.txt", "shared/../../escape.txt", "shared//etc/passwd", "shared/~/x"]


@pytest.mark.parametrize("tool", ["read_file", "write_file", "edit_file", "list_files", "search_files"])
@pytest.mark.parametrize("path", EVIL)
def test_escape_refused(env, tool, path):
    out = call(env, tool, path=path, content="pwn", old="a", new="b", pattern="x")
    assert out.startswith("ERROR"), out
    assert not (env.ws / "escape.txt").exists()
    assert not (env.tmp / "escape.txt").exists()
    assert events(env, "agentfs.denied")


def test_diff_escape_refused(env):
    call(env, "write_file", path="a.txt", content="x")
    assert call(env, "diff_files", path_a="a.txt", path_b="../../x").startswith("ERROR")
    assert call(env, "diff_files", path_a="/etc/passwd", text_b="x").startswith("ERROR")


def test_symlink_escape_blocked(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "t.txt").write_text("host secret")
    os.symlink(outside, env.dir / "link")
    os.symlink(outside / "t.txt", env.dir / "flink")
    os.symlink(outside, env.ws / "slink")
    for path in ("link/t.txt", "flink", "shared/slink/t.txt"):
        assert call(env, "read_file", path=path).startswith("ERROR"), path
        assert call(env, "write_file", path=path, content="x").startswith("ERROR"), path
        assert call(env, "edit_file", path=path, old="host", new="pwn").startswith("ERROR"), path
    assert call(env, "write_file", path="link/new.txt", content="x").startswith("ERROR")
    assert call(env, "list_files", path="link").startswith("ERROR")
    assert (outside / "t.txt").read_text() == "host secret"
    assert not (outside / "new.txt").exists()


def test_symlink_inside_agent_dir_to_own_file_is_not_written(env):
    call(env, "write_file", path="real.txt", content="r")
    os.symlink(env.dir / "real.txt", env.dir / "alias")
    # resolves to real.txt (inside), so the write goes to the real file
    assert call(env, "write_file", path="alias", content="new").startswith("wrote real.txt")


# ------------------------------------------------------------------ quota / file cap
def test_write_file_honours_file_cap(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "1")
    out = call(env, "write_file", path="big.bin", content="a" * (MB + 1))
    assert out.startswith("ERROR") and "per-file cap" in out
    assert not (env.dir / "big.bin").exists()
    assert events(env, "agentfs.denied")[-1]["data"]["tool"] == "write_file"


def test_write_file_honours_quota(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    assert call(env, "write_file", path="a.bin", content="a" * (MB // 2)).startswith("wrote")
    out = call(env, "write_file", path="b.bin", content="b" * (MB // 2 + 10))
    assert "quota exceeded" in out
    # overwriting an existing file counts only the delta
    assert call(env, "write_file", path="a.bin", content="a" * (MB // 2 + 10)).startswith("wrote")


def test_edit_file_honours_quota(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "1")
    call(env, "write_file", path="e.txt", content="X" + "a" * (MB - 10))
    out = call(env, "edit_file", path="e.txt", old="X", new="Y" * 100)
    assert "per-file cap" in out
    assert (env.dir / "e.txt").read_text().startswith("X")
    assert events(env, "agentfs.denied")[-1]["data"]["tool"] == "edit_file"


def test_shared_writes_are_not_charged_to_agent_quota(env, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    assert call(env, "write_file", path="shared/big.bin", content="a" * (MB + 5)).startswith("wrote shared/")


def test_write_file_refuses_directory_target(env):
    (env.dir / "sub").mkdir()
    assert "not a regular file" in call(env, "write_file", path="sub", content="x")
    assert "must name a file" in call(env, "write_file", path=".", content="x")


# ------------------------------------------------------------------ disabled / legacy mode
@pytest.mark.parametrize("mode", ["env_off", "no_dir"])
def test_disabled_mode_is_old_behaviour(env, monkeypatch, mode):
    ctx = env.ctx
    if mode == "env_off":
        monkeypatch.setenv("AVRONE_AGENT_FS", "0")
    else:
        ctx = env.legacy
    out = call(env, "write_file", ctx=ctx, path="/x/y.txt", content="hi")   # leading / stripped
    assert out == "wrote x/y.txt (2 bytes)"
    assert (env.ws / "x" / "y.txt").read_text() == "hi"
    # "shared/" is just a directory name in legacy mode
    call(env, "write_file", ctx=ctx, path="shared/z.txt", content="z")
    assert (env.ws / "shared" / "z.txt").exists()
    assert call(env, "read_file", ctx=ctx, path="x/y.txt") == "hi"
    assert call(env, "edit_file", ctx=ctx, path="x/y.txt", old="hi", new="yo") == "edited x/y.txt (1 replacement)"
    assert "agents/" in call(env, "list_files", ctx=ctx)
    assert call(env, "search_files", ctx=ctx, pattern="yo") == "x/y.txt:1: yo"
    assert call(env, "read_file", ctx=ctx, path="../../etc/passwd").startswith("ERROR")
    assert not events(env, "agentfs.write")


def test_legacy_ctx_without_agent_dir_attr(env):
    ctx = SimpleNamespace(core=env.ctx.core, run_id="run1", name="Old", depth=0)
    assert call(env, "write_file", ctx=ctx, path="o.txt", content="o") == "wrote o.txt (1 bytes)"
    assert fsunify.agent_root(ctx) is None


# ------------------------------------------------------------------ helpers
def test_display_paths(env):
    assert fsunify.display(env.ctx, env.dir) == "."
    assert fsunify.display(env.ctx, env.ws) == "shared/"
    assert fsunify.display(env.ctx, env.ws / "q.txt") == "shared/q.txt"
    assert fsunify.display(env.ctx, env.tmp / "elsewhere") == str(env.tmp / "elsewhere")
    assert fsunify.display(env.legacy, env.ws / "q.txt") == "q.txt"
    assert fsunify.display(env.legacy, env.tmp / "elsewhere") == str(env.tmp / "elsewhere")


def test_agent_root_created_on_demand(env, tmp_path):
    d = tmp_path / "ws" / "agents" / "gamma"
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_dir": d})
    assert fsunify.agent_root(ctx) == d.resolve() and d.is_dir()


def test_audit_never_breaks(env):
    class Boom:
        def emit(self, *a, **k):
            raise RuntimeError("codex down")
    core = SimpleNamespace(**{**vars(env.ctx.core), "codex": Boom()})
    ctx = SimpleNamespace(**{**vars(env.ctx), "core": core})
    assert call(env, "read_file", ctx=ctx, path="../x").startswith("ERROR")
    core2 = SimpleNamespace(settings=env.s, runner=env.ctx.core.runner)  # no codex at all
    ctx2 = SimpleNamespace(**{**vars(env.ctx), "core": core2})
    assert call(env, "read_file", ctx=ctx2, path="../x").startswith("ERROR")


def test_skip_dirs(env):
    r_own = fsunify.resolve(env.ctx, "", "t")
    r_shared = fsunify.resolve(env.ctx, "shared", "t")
    r_legacy = fsunify.resolve(env.legacy, "", "t")
    assert fsunify.skip_dirs(env.ctx, r_own) == set()
    assert fsunify.skip_dirs(env.legacy, r_legacy) == set()
    assert fsunify.skip_dirs(env.ctx, r_shared) == {env.ws / "agents"}


def test_registry_unchanged():
    reg = ToolRegistry()
    register_extra_tools(reg)
    assert {"read_file", "write_file"}.isdisjoint(reg.names())
    assert {"read_file", "write_file"} <= set(builtin_registry().names())


# ------------------------------------------------------------------ python cwd
def test_python_runs_in_agent_dir(env):
    call(env, "write_file", path="data.txt", content="agent data")
    out = call(env, "python", code="import os; print(os.getcwd()); print(open('data.txt').read())")
    assert str(env.dir) in out and "agent data" in out
    assert not list(env.dir.glob(".snippet_*")), "snippet must be cleaned up"
    assert not (env.ws / ".avrone" / "tmp").exists()


def test_python_sees_shared_workspace_env(env):
    out = call(env, "python", code="import os; print(os.environ['AVRONE_SHARED_WORKSPACE'])")
    assert str(env.ws) in out


@pytest.mark.parametrize("mode", ["env_off", "no_dir"])
def test_python_disabled_mode_runs_in_workspace(env, monkeypatch, mode):
    ctx = env.ctx
    if mode == "env_off":
        monkeypatch.setenv("AVRONE_AGENT_FS", "0")
    else:
        ctx = env.legacy
    out = call(env, "python", ctx=ctx, code="import os; print(os.getcwd())")
    assert out.strip().splitlines()[-1] == str(env.ws)
    assert str(env.dir) not in out


def test_python_runner_without_cwd_falls_back(env):
    seen = []

    class OldRunner:
        workspace = env.ws

        async def run(self, cmd, timeout=None):
            seen.append(cmd)
            return SimpleNamespace(text=lambda: "ok")

    core = SimpleNamespace(settings=env.s, runner=OldRunner(), codex=env.codex)
    ctx = SimpleNamespace(**{**vars(env.ctx), "core": core})
    assert call(env, "python", ctx=ctx, code="print(1)") == "ok"
    assert seen and ".avrone/tmp/snippet_" in seen[0]


@pytest.mark.parametrize("isolate", ["0", "1"])
def test_python_docker_paths(env, monkeypatch, isolate):
    from avrone.sovereign import DockerRunner
    monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", isolate)
    r = DockerRunner(env.s)
    calls = []

    async def fake_exec(argv, cmd, timeout, cwd=None, env_=None, **kw):
        calls.append((argv, [p.name for p in env_dir.glob(".snippet_*.py")]))
        return SimpleNamespace(text=lambda: "docker ok")

    env_dir = env.dir
    monkeypatch.setattr(r, "_exec", fake_exec)
    core = SimpleNamespace(settings=env.s, runner=r, codex=env.codex)
    ctx = SimpleNamespace(**{**vars(env.ctx), "core": core})
    assert call(env, "python", ctx=ctx, code="print(1)") == "docker ok"
    argv, present = calls[0]
    assert len(present) == 1 and argv[-1] == f"python3 {present[0]}"
    if isolate == "1":
        assert "-w" in argv and argv[argv.index("-w") + 1] == "/agent"
        assert f"{env.dir}:/agent" in argv
    else:
        assert argv[:4] == ["docker", "exec", "-w", "/workspace/agents/alpha"]
    assert not list(env.dir.glob(".snippet_*"))


def test_runner_accepts_cwd():
    from avrone.tools import runner_accepts_cwd

    class R:
        async def run(self, cmd, cwd=None):
            return None

    class Bad:
        run = 42

    assert runner_accepts_cwd(R()) is True
    assert runner_accepts_cwd(Bad()) is False


def test_tool_docs_mention_shared():
    reg = builtin_registry()
    register_extra_tools(reg)
    docs = reg.docs()
    for name in ("read_file", "write_file", "edit_file", "list_files", "search_files", "diff_files"):
        line = next(ln for ln in docs.splitlines() if ln.startswith(f"- {name} "))
        assert "shared/" in line, name
    assert "agent dir" in next(ln for ln in docs.splitlines() if ln.startswith("- python "))
