"""Round 25: edit_file and the write path against real-world files: latin-1 / mixed bytes
(previously silently rewritten as U+FFFD), CRLF files with an LF `old`, trailing-whitespace
drift, misses that should point at the closest line, binary files, directories, replace_all,
identical old/new, atomic writes, and file modes (round 24's atomic write made files 0600)."""
import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from avrone import agentfs
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry, builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


@pytest.fixture(params=["legacy", "agent"])
def env(request, tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_FS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "d")
    r = LocalRunner(s)
    run(r.start())
    core = SimpleNamespace(settings=s, runner=r, codex=Codex(s.data_dir))
    if request.param == "agent":
        d = agentfs.agent_dir(s.workspace, "alpha")
        ctx = SimpleNamespace(core=core, run_id="r", name="A", depth=0, agent_id="alpha", agent_dir=d)
        root = d
    else:
        ctx = SimpleNamespace(core=core, run_id="r", name="X", depth=0)
        root = s.workspace
    root.mkdir(parents=True, exist_ok=True)
    reg = builtin_registry()
    register_extra_tools(reg)
    return SimpleNamespace(root=root, ctx=ctx, reg=reg, kind=request.param)


def call(env, name, **a):
    return run(env.reg.call(env.ctx, name, a))


def edit(env, path, old, new, **kw):
    return call(env, "edit_file", path=path, old=old, new=new, **kw)


def test_basic_unique_edit(env):
    (env.root / "a.py").write_text("x = 1\ny = 2\n")
    assert edit(env, "a.py", "y = 2", "y = 3") == "edited a.py (1 replacement)"
    assert (env.root / "a.py").read_text() == "x = 1\ny = 3\n"


def test_latin1_bytes_elsewhere_survive(env):
    raw = b"caf\xe9 = 1\nvalue = 2\nna\xefve = 3\n"
    (env.root / "l.py").write_bytes(raw)
    assert edit(env, "l.py", "value = 2", "value = 9").startswith("edited")
    assert (env.root / "l.py").read_bytes() == raw.replace(b"value = 2", b"value = 9")


def test_utf8_bom_preserved(env):
    raw = "\ufeffa = 1\nb = 2\n".encode("utf-8")
    (env.root / "bom.py").write_bytes(raw)
    edit(env, "bom.py", "b = 2", "b = 5")
    assert (env.root / "bom.py").read_bytes().startswith(b"\xef\xbb\xbf")


def test_crlf_file_lf_old_multiline(env):
    (env.root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert edit(env, "w.txt", "two\nthree", "TWO\nTHREE").startswith("edited")
    assert (env.root / "w.txt").read_bytes() == b"one\r\nTWO\r\nTHREE\r\n"


def test_crlf_file_single_line_old(env):
    (env.root / "w.txt").write_bytes(b"alpha\r\nbeta\r\n")
    edit(env, "w.txt", "beta", "gamma")
    assert (env.root / "w.txt").read_bytes() == b"alpha\r\ngamma\r\n"


def test_crlf_new_with_crlf_not_doubled(env):
    (env.root / "w.txt").write_bytes(b"a\r\nb\r\n")
    edit(env, "w.txt", "a\nb", "x\r\ny")
    assert (env.root / "w.txt").read_bytes() == b"x\r\ny\r\n"


def test_trailing_whitespace_drift_matches(env):
    (env.root / "t.py").write_text("def f():   \n    return 1  \nprint(f())\n")
    out = edit(env, "t.py", "def f():\n    return 1", "def f():\n    return 2")
    assert "ignoring trailing whitespace" in out
    assert (env.root / "t.py").read_text() == "def f():\n    return 2\nprint(f())\n"


def test_loose_match_never_splices_mid_line(env):
    (env.root / "m.txt").write_text("prefix value  \n")
    out = edit(env, "m.txt", "value", "X")  # exact match exists; fine
    assert out.startswith("edited")
    (env.root / "n.txt").write_text("abc value def\n")
    assert edit(env, "n.txt", "value  \nzzz", "Q").startswith("ERROR: 'old' text not found")
    assert (env.root / "n.txt").read_text() == "abc value def\n"


def test_loose_ambiguous_refused(env):
    (env.root / "d.txt").write_text("x = 1 \ny\nx = 1  \n")
    out = edit(env, "d.txt", "x = 1\n", "x = 2\n")
    assert out.startswith("ERROR") and "2 times" in out


def test_miss_names_closest_line(env):
    (env.root / "c.py").write_text("import os\n\ndef compute_total(items):\n    return sum(items)\n")
    out = edit(env, "c.py", "def compute_totals(items):", "def total(items):")
    assert out.startswith("ERROR: 'old' text not found") and "closest line is 3" in out


def test_miss_without_close_line_has_no_hint(env):
    (env.root / "c.py").write_text("aaa\n")
    out = edit(env, "c.py", "zzzzzzzz qqqq", "b")
    assert out == "ERROR: 'old' text not found in c.py"


def test_multiple_matches_needs_replace_all(env):
    (env.root / "r.txt").write_text("foo bar foo baz foo\n")
    out = edit(env, "r.txt", "foo", "qux")
    assert "matches 3 times" in out and "replace_all=true" in out
    assert edit(env, "r.txt", "foo", "qux", replace_all=True) == "edited r.txt (3 replacements)"
    assert (env.root / "r.txt").read_text() == "qux bar qux baz qux\n"


@pytest.mark.parametrize("flag", ["true", "YES", "1", "all"])
def test_replace_all_string_flags(env, flag):
    (env.root / "r.txt").write_text("a a\n")
    assert env.reg.check("edit_file", {"path": "r.txt", "old": "a", "new": "b", "replace_all": flag}) is None
    assert "2 replacements" in edit(env, "r.txt", "a", "b", replace_all=flag)


def test_replace_all_false_string_is_false(env):
    (env.root / "r.txt").write_text("a a\n")
    assert "matches 2 times" in edit(env, "r.txt", "a", "b", replace_all="false")


def test_identical_old_new_refused(env):
    (env.root / "s.txt").write_text("same\n")
    assert "identical" in edit(env, "s.txt", "same", "same")


def test_binary_refused_and_untouched(env):
    raw = b"\x00\x01ELF abc"
    (env.root / "b.bin").write_bytes(raw)
    assert "binary file" in edit(env, "b.bin", "abc", "xyz")
    assert (env.root / "b.bin").read_bytes() == raw


def test_directory_refused(env):
    (env.root / "dd").mkdir()
    assert "is a directory" in edit(env, "dd", "a", "b")


def test_empty_path(env):
    assert edit(env, "", "a", "b") == "ERROR: edit_file needs a file path"


def test_missing_file(env):
    assert edit(env, "nope.txt", "a", "b").endswith("not found")


def test_none_new_deletes_text(env):
    (env.root / "x.txt").write_text("keep DROP keep\n")
    run(env.reg.call(env.ctx, "edit_file", {"path": "x.txt", "old": " DROP", "new": None}))
    assert (env.root / "x.txt").read_text() == "keep keep\n"


def test_lone_surrogate_in_new_becomes_replacement(env):
    (env.root / "x.txt").write_text("a\n")
    assert edit(env, "x.txt", "a", "b\udcffc").startswith("edited")
    assert (env.root / "x.txt").read_text(encoding="utf-8") == "b\ufffdc\n"


def test_exec_bit_survives_edit(env):
    f = env.root / "run.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    os.chmod(f, 0o755)
    edit(env, "run.sh", "echo hi", "echo bye")
    assert stat.S_IMODE(f.stat().st_mode) & 0o100


def test_exec_bit_survives_write_file(env):
    f = env.root / "run.sh"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o755)
    call(env, "write_file", path="run.sh", content="#!/bin/sh\necho 2\n")
    assert stat.S_IMODE(f.stat().st_mode) & 0o100


def test_new_shared_file_not_0600(env):
    if env.kind == "agent":
        pytest.skip("agent dirs are private by design")
    call(env, "write_file", path="new.txt", content="x")
    um = os.umask(0); os.umask(um)
    assert stat.S_IMODE((env.root / "new.txt").stat().st_mode) == 0o666 & ~um


def test_edit_atomic_on_failure(env, monkeypatch):
    f = env.root / "k.txt"
    f.write_text("orig\n")
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    out = edit(env, "k.txt", "orig", "new")
    assert out.startswith("ERROR")
    assert f.read_text() == "orig\n"
    assert not [p for p in env.root.iterdir() if ".tmp" in p.name]
