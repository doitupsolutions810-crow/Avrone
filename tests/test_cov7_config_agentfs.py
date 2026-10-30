"""Round-7 coverage: config._load_dotenv parsing and agentfs_tools error/edge branches."""
import errno
import os
from types import SimpleNamespace

import pytest

from avrone import agentfs_tools as aft
from avrone import config
from avrone.agentfs import AgentFSError

from tests.test_agent_fs_tools import call, env, events, run  # noqa: F401  (env is a fixture)


# ------------------------------------------------------------------ config._load_dotenv
DOTENV_KEYS = ("AVR7_PLAIN", "AVR7_DQ", "AVR7_SQ", "AVR7_TRAIL", "AVR7_SPACED", "AVR7_KEEP",
               "AVR7_EQ", "AVR7_EMPTY", "AVR7_HASHVAL")


@pytest.fixture()
def clean_env(monkeypatch):
    for k in DOTENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_load_dotenv_missing_file_is_noop(tmp_path, clean_env):
    before = dict(os.environ)
    config._load_dotenv(tmp_path / "nope.env")
    assert dict(os.environ) == before


def test_load_dotenv_parses_lines(tmp_path, clean_env):
    clean_env.setenv("AVR7_KEEP", "from-real-env")
    f = tmp_path / ".env"
    f.write_text(
        "# a comment line\n"
        "\n"
        "   \n"
        "NOT_A_PAIR_LINE\n"
        "AVR7_PLAIN=hello\n"
        'AVR7_DQ="double quoted"\n'
        "AVR7_SQ='single'\n"
        "AVR7_TRAIL=value # trailing comment\n"
        "  AVR7_SPACED  =  padded  \n"
        "AVR7_KEEP=from-dotenv\n"
        "AVR7_EQ=a=b=c\n"
        "AVR7_EMPTY=\n"
        "AVR7_HASHVAL=abc#def\n"
    )
    config._load_dotenv(f)
    assert os.environ["AVR7_PLAIN"] == "hello"
    assert os.environ["AVR7_DQ"] == "double quoted"
    assert os.environ["AVR7_SQ"] == "single"
    assert os.environ["AVR7_TRAIL"] == "value"
    assert os.environ["AVR7_SPACED"] == "padded"
    assert os.environ["AVR7_KEEP"] == "from-real-env"  # setdefault: real env wins
    assert os.environ["AVR7_EQ"] == "a=b=c"  # only the first '=' splits
    assert os.environ["AVR7_EMPTY"] == ""
    assert os.environ["AVR7_HASHVAL"] == "abc#def"  # '#' without a leading space is data
    assert "NOT_A_PAIR_LINE" not in os.environ


def test_dotenv_values_feed_fresh_settings(tmp_path, clean_env):
    for k in ("AVRONE_PROVIDER", "AVRONE_LLM_MODEL", "AVRONE_MAX_STEPS"):
        clean_env.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("AVRONE_PROVIDER=ollama\nAVRONE_MAX_STEPS=9 # more steps\n")
    config._load_dotenv(f)
    s = config.Settings()
    assert s.provider == "ollama" and s.llm_model == "qwen2.5:3b" and s.max_steps == 9
    assert s.llm_base_url.startswith("http://127.0.0.1:11434")


def test_resolve_provider_branches():
    assert config.resolve_provider(" XAI ", env={"XAI_API_KEY": "k"}) == (
        "xai", "https://api.x.ai/v1", "grok-4", "k")
    # explicit key beats env; explicit url/model beat preset
    assert config.resolve_provider("xai", "http://x/v1", "m", "explicit", env={"XAI_API_KEY": "k"}) == (
        "xai", "http://x/v1", "m", "explicit")
    # ollama has no key env var
    assert config.resolve_provider("ollama", env={"OLLAMA": "z"})[3] == ""
    assert config.resolve_provider(None, env={})[0] == "openclaw"
    with pytest.raises(ValueError, match="custom needs"):
        config.resolve_provider("custom", env={})
    with pytest.raises(ValueError, match="Unknown AVRONE_PROVIDER"):
        config.resolve_provider("nope", env={})


def test_settings_bool_and_path_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "openclaw")
    monkeypatch.setenv("AVRONE_GOVERNOR", "off")
    monkeypatch.setenv("AVRONE_ALLOW_NETWORK", "YES")
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "w" / ".." / "w2"))
    s = config.Settings()
    assert s.governor is False and s.allow_network is True
    assert s.workspace == (tmp_path / "w2").resolve()


# ------------------------------------------------------------------ agentfs_tools: env clamping
@pytest.mark.parametrize("raw,expected_mb", [
    ("", aft.DEFAULT_QUOTA_MB), ("   ", aft.DEFAULT_QUOTA_MB), ("junk", aft.DEFAULT_QUOTA_MB),
    ("0", aft.DEFAULT_QUOTA_MB), ("-3", aft.DEFAULT_QUOTA_MB), ("2.5", 2.5),
    ("99999", aft.HARD_QUOTA_MB), ("inf", aft.HARD_QUOTA_MB), ("nan", aft.DEFAULT_QUOTA_MB),
    ("NaN", aft.DEFAULT_QUOTA_MB),
])
def test_quota_env_clamping(monkeypatch, raw, expected_mb):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", raw)
    assert aft.quota_bytes() == int(expected_mb * aft.MB)


def test_max_file_never_exceeds_quota(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "2")
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "60")
    assert aft.max_file_bytes() == 2 * aft.MB
    monkeypatch.setenv("AVRONE_AGENT_FS_MAX_FILE_MB", "500")
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1000")
    assert aft.max_file_bytes() == aft.HARD_FILE_MB * aft.MB


# ------------------------------------------------------------------ dir_usage / audit
def test_dir_usage_skips_files_that_vanish(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"x" * 10)
    (tmp_path / "gone").write_bytes(b"y" * 99)
    real = os.lstat

    def flaky(p, *a, **kw):
        if str(p).endswith("gone"):
            raise FileNotFoundError(p)
        return real(p, *a, **kw)

    monkeypatch.setattr(aft.os, "lstat", flaky)
    assert aft.dir_usage(tmp_path) == 10


def test_dir_usage_counts_symlink_not_target(tmp_path):
    big = tmp_path / "outside.bin"
    big.write_bytes(b"z" * 5000)
    d = tmp_path / "d"
    d.mkdir()
    os.symlink(big, d / "link")
    assert aft.dir_usage(d) == os.lstat(d / "link").st_size < 5000


def test_audit_failure_never_breaks_tool(env):
    class Boom:
        def emit(self, *a, **kw):
            raise RuntimeError("codex down")

    ctx = SimpleNamespace(**{**vars(env.ctx), "core": SimpleNamespace(codex=Boom())})
    assert call(env, "fs_write", ctx=ctx, path="a.txt", content="hi").startswith("wrote a.txt")
    assert call(env, "fs_read", ctx=ctx, path="/etc/passwd").startswith("ERROR")


def test_audit_without_codex_is_silent(env):
    ctx = SimpleNamespace(**{**vars(env.ctx), "core": SimpleNamespace()})
    assert call(env, "fs_write", ctx=ctx, path="b.txt", content="ok").startswith("wrote b.txt")


# ------------------------------------------------------------------ quota_write edge branches
def test_quota_write_refuses_root_itself(env):
    with pytest.raises(AgentFSError, match="must name a file"):
        aft.quota_write(env.ctx, env.dir, env.dir, b"x")


def test_quota_write_rechecks_parent_after_mkdir(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    os.symlink(outside, env.dir / "lnk")
    with pytest.raises(AgentFSError, match="escapes"):
        aft.quota_write(env.ctx, env.dir, env.dir / "lnk" / "f.txt", b"x")
    assert not (outside / "f.txt").exists()


def test_quota_write_cleans_tmp_when_replace_fails(env, monkeypatch):
    def bad_replace(src, dst):
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(aft.os, "replace", bad_replace)
    with pytest.raises(OSError) as ei:
        aft.quota_write(env.ctx, env.dir, env.dir / "f.txt", b"data")
    assert not isinstance(ei.value, AgentFSError) and ei.value.errno == errno.EIO
    assert list(env.dir.iterdir()) == []  # temp file removed, target never created


def test_quota_write_tmp_cleanup_tolerates_missing_tmp(env, monkeypatch):
    def replace_and_unlink(src, dst):
        os.unlink(src)
        raise OSError(errno.EIO, "boom")

    monkeypatch.setattr(aft.os, "replace", replace_and_unlink)
    with pytest.raises(OSError):
        aft.quota_write(env.ctx, env.dir, env.dir / "g.txt", b"data")
    assert list(env.dir.iterdir()) == []


def test_quota_write_symlink_planted_at_leaf_is_refused(env, monkeypatch):
    outside = env.tmp / "o.txt"
    outside.write_text("secret")
    real_open = os.open

    def plant_then_open(path, flags, *a):
        if str(path) == str(env.dir / "leaf.txt") and not os.path.lexists(path):
            os.symlink(outside, path)
        return real_open(path, flags, *a)

    monkeypatch.setattr(aft.os, "open", plant_then_open)
    with pytest.raises(AgentFSError, match="symlink"):
        aft.quota_write(env.ctx, env.dir, env.dir / "leaf.txt", b"pwn", append=True)
    assert outside.read_text() == "secret"
    assert any(e["data"].get("reason") == "symlink" for e in events(env, "agentfs.denied"))


def test_quota_write_other_tool_name_is_audited(env):
    aft.quota_write(env.ctx, env.dir, env.dir / "w.txt", b"abc", tool="write_file")
    ev = events(env, "agentfs.write")
    assert ev[-1]["data"]["tool"] == "write_file" and ev[-1]["data"]["bytes"] == 3


# ------------------------------------------------------------------ read / list / delete branches
@pytest.mark.parametrize("bad", ["lots", None, [1]])
def test_read_bad_max_chars_falls_back_to_default(env, bad):
    call(env, "fs_write", path="r.txt", content="abc")
    assert call(env, "fs_read", path="r.txt", max_chars=bad) == "abc"


def test_read_max_chars_clamped_low(env):
    call(env, "fs_write", path="r.txt", content="abcdef")
    out = call(env, "fs_read", path="r.txt", max_chars=-5)
    assert out.startswith("a\n...[truncated") and "6 bytes" in out


def test_read_multibyte_truncation(env):
    call(env, "fs_write", path="u.txt", content="é" * 10)
    out = call(env, "fs_read", path="u.txt", max_chars=3)
    assert out.startswith("ééé\n...[truncated; file is 20 bytes]")


def test_list_missing_and_single_file(env):
    assert call(env, "fs_list", path="nope") == "ERROR: nope not found"
    call(env, "fs_write", path="one.txt", content="12345")
    assert call(env, "fs_list", path="one.txt") == "one.txt  5 bytes"


def test_list_empty_and_marks_symlinks_and_dirs(env):
    assert call(env, "fs_list").endswith("(empty)")
    call(env, "fs_write", path="sub/x.txt", content="1")
    os.symlink(env.tmp, env.dir / "ln")
    out = call(env, "fs_list")
    assert out.startswith("[agent dir alpha:")
    assert "ln -> (symlink)" in out and "sub/" in out and "x.txt" not in out
    rec = call(env, "fs_list", recursive=True)
    assert "sub/x.txt  1 bytes" in rec


def test_list_header_falls_back_to_dir_name(env):
    ctx = SimpleNamespace(**{**vars(env.ctx), "agent_id": ""})
    assert call(env, "fs_list", ctx=ctx).startswith(f"[agent dir {env.dir.name}:")


def test_list_truncates_many_entries(env, monkeypatch):
    monkeypatch.setattr(aft, "LIST_MAX_ENTRIES", 3)
    for i in range(5):
        (env.dir / f"f{i}").write_text("x")
    out = call(env, "fs_list")
    assert out.endswith("...[2 more]")
    assert "f0" in out and "f2" in out and "f3" not in out


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_delete_special_file_refused(env):
    os.mkfifo(env.dir / "pipe")
    assert call(env, "fs_delete", path="pipe") == "ERROR: pipe is not a file or directory"
    assert os.path.exists(env.dir / "pipe")


def test_delete_empty_dir_without_recursive(env):
    (env.dir / "empty").mkdir()
    assert call(env, "fs_delete", path="empty/") == "deleted empty (0 bytes freed)"
    assert not (env.dir / "empty").exists()


def test_delete_trailing_dotdot_refused(env):
    out = call(env, "fs_delete", path="sub/..")
    assert out.startswith("ERROR") and "'..'" in out
    assert events(env, "agentfs.denied")


def test_delete_frees_bytes_and_audits(env):
    call(env, "fs_write", path="d/a", content="aaaa")
    call(env, "fs_write", path="d/b", content="bb")
    assert call(env, "fs_delete", path="d", recursive=True) == "deleted d (6 bytes freed)"
    assert events(env, "agentfs.delete")[-1]["data"] == {"agent_id": "alpha", "path": "d", "bytes": 6}
