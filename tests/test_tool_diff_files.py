import asyncio
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", agents_can_shell=False, allow_network=False)
    runner = LocalRunner(s)
    run(runner.start())
    core = SimpleNamespace(settings=s, runner=runner, codex=Codex(s.data_dir))
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    return SimpleNamespace(ws=s.workspace, ctx=ctx, reg=reg, tmp=tmp_path)


def call(env, **args):
    return run(env.reg.call(env.ctx, "diff_files", args))


def test_registered_with_doc(env):
    t = env.reg.get("diff_files")
    assert t is not None and t.source == "builtin"
    assert "path_a" in t.args_doc and "text_b" in t.args_doc


def test_two_files(env):
    (env.ws / "a.txt").write_text("one\ntwo\nthree\n")
    (env.ws / "sub").mkdir()
    (env.ws / "sub" / "b.txt").write_text("one\nTWO\nthree\nfour\n")
    out = call(env, path_a="a.txt", path_b="sub/b.txt")
    assert "--- a.txt" in out and "+++ sub/b.txt" in out
    assert "-two" in out and "+TWO" in out and "+four" in out
    assert out.endswith("[2 added, 1 removed]")


def test_file_vs_text_and_context(env):
    (env.ws / "a.txt").write_text("".join(f"l{i}\n" for i in range(20)))
    new = "".join(f"l{i}\n" if i != 10 else "CHANGED\n" for i in range(20))
    out = call(env, path_a="a.txt", text_b=new, context=0)
    assert "+++ (text_b)" in out and "-l10" in out and "+CHANGED" in out
    assert " l9" not in out  # zero context lines
    assert " l9" in call(env, path_a="a.txt", text_b=new)  # default 3 context lines


def test_identical(env):
    (env.ws / "a.txt").write_text("same\n")
    (env.ws / "b.txt").write_text("same\n")
    assert call(env, path_a="a.txt", path_b="b.txt").startswith("No differences")
    assert call(env, path_a="a.txt", text_b="same\n").startswith("No differences")


def test_missing_newline_marker(env):
    (env.ws / "a.txt").write_text("x\n")
    out = call(env, path_a="a.txt", text_b="y")
    assert "\\ No newline at end of file" in out


def test_output_capped(env):
    (env.ws / "a.txt").write_text("".join(f"old line {i}\n" for i in range(3000)))
    out = call(env, path_a="a.txt", text_b="")
    assert "diff truncated" in out
    assert len(out) < tx.DIFF_MAX_OUTPUT_CHARS + 200


def test_argument_errors(env):
    (env.ws / "a.txt").write_text("x\n")
    assert "exactly one" in call(env, path_a="a.txt")
    assert "exactly one" in call(env, path_a="a.txt", path_b="a.txt", text_b="x")
    assert "path_a is required" in call(env, text_b="x")
    assert "text_b must be a string" in call(env, path_a="a.txt", text_b=123)


def test_path_errors(env):
    (env.ws / "a.txt").write_text("x\n")
    (env.ws / "d").mkdir()
    (env.ws / "bin.dat").write_bytes(b"ab\0cd")
    assert "not found" in call(env, path_a="nope.txt", text_b="x")
    assert "not found" in call(env, path_a="a.txt", path_b="nope.txt")
    assert "not a file" in call(env, path_a="d", text_b="x")
    assert "binary" in call(env, path_a="bin.dat", text_b="x")
    assert "binary" in call(env, path_a="a.txt", path_b="bin.dat")


def test_escape_rejected(env):
    (env.tmp / "secret.txt").write_text("s\n")
    (env.ws / "a.txt").write_text("x\n")
    assert "escapes" in call(env, path_a="../secret.txt", text_b="x")
    assert "escapes" in call(env, path_a="a.txt", path_b="../../secret.txt")
    (env.ws / "link.txt").symlink_to(env.tmp / "secret.txt")
    assert "escapes" in call(env, path_a="link.txt", text_b="x")


def test_size_limits(env, monkeypatch):
    (env.ws / "a.txt").write_text("x" * 100)
    monkeypatch.setattr(tx, "DIFF_MAX_FILE_BYTES", 50)
    assert "larger than" in call(env, path_a="a.txt", text_b="x")
    monkeypatch.setattr(tx, "DIFF_MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(tx, "DIFF_MAX_TEXT_CHARS", 5)
    assert "longer than" in call(env, path_a="a.txt", text_b="y" * 10)
