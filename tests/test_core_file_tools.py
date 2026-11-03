"""Round 24: the core read_file / write_file / fetch tools against what agents actually do:
read a directory, a binary, a huge log, page through a long file; write a dict to a .json,
write text with a lone surrogate, write to '' or a directory; fetch a binary, a huge body,
a URL wrapped in quotes or <>, HTML entities, timeouts. Legacy ctx (no agent dir)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from avrone import tools
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import builtin_registry, clean_url, write_content


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def env(tmp_path):
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "d")
    r = LocalRunner(s)
    run(r.start())
    core = SimpleNamespace(settings=s, runner=r, codex=Codex(s.data_dir))
    ctx = SimpleNamespace(core=core, run_id="r", name="X", depth=0)
    reg = builtin_registry()
    return SimpleNamespace(ws=s.workspace, ctx=ctx, reg=reg, s=s)


def call(env, name, **a):
    return run(env.reg.call(env.ctx, name, a))


def check(env, name, **a):
    return env.reg.check(name, a)


# --- read_file ------------------------------------------------------------------------------------

def test_read_small_file_unchanged(env):
    (env.ws / "a.txt").write_text("hello\n")
    assert call(env, "read_file", path="a.txt") == "hello\n"


def test_read_directory_lists_entries(env):
    (env.ws / "sub").mkdir(); (env.ws / "sub" / "x.py").write_text("1"); (env.ws / "sub" / "d").mkdir()
    out = call(env, "read_file", path="sub")
    assert out.startswith("ERROR: sub is a directory") and "x.py" in out and "d/" in out
    assert "IsADirectoryError" not in out


def test_read_empty_dir_listing(env):
    (env.ws / "e").mkdir()
    assert "(empty)" in call(env, "read_file", path="e")


@pytest.mark.parametrize("p", ["", "   ", None])
def test_read_needs_a_path(env, p):
    out = call(env, "read_file", path=p)
    assert out.startswith("ERROR: read_file needs a file path")


def test_read_binary_refused(env):
    (env.ws / "b.bin").write_bytes(b"\x7fELF\x00\x01\x02" * 100)
    out = call(env, "read_file", path="b.bin")
    assert out.startswith("ERROR: b.bin is a binary file (700 bytes)") and "\x00" not in out


def test_read_truncation_is_announced_with_offset(env):
    (env.ws / "big.log").write_text("x" * 30000)
    out = call(env, "read_file", path="big.log")
    assert out.startswith("x" * 12000)
    assert "[truncated: showing chars 0-12000 of a 30000-byte file; call read_file with offset=12000" in out


def test_read_paging_reaches_the_end(env):
    body = "".join(f"{i:05d}\n" for i in range(5000))  # 30000 chars
    (env.ws / "n.txt").write_text(body)
    got, off = "", 0
    for _ in range(5):
        out = call(env, "read_file", path="n.txt", offset=off)
        text, _, note = out.rpartition("\n[")
        got += text
        if note.startswith("end of file"):
            break
        off = int(note.split("offset=")[1].split()[0].rstrip("]"))
    assert got == body


@pytest.mark.parametrize("off", ["12000", " 12000 ", 12000])
def test_read_offset_accepts_string_ints(env, off):
    (env.ws / "big").write_text("a" * 12000 + "b" * 50)
    assert check(env, "read_file", path="big", offset=off) is None
    out = call(env, "read_file", path="big", offset=off)
    assert out.startswith("b" * 50) and "end of file" in out


def test_read_offset_past_end(env):
    (env.ws / "s").write_text("abc")
    assert "past the end" in call(env, "read_file", path="s", offset=99)


def test_read_junk_offset_is_zero(env):
    (env.ws / "s").write_text("abc")
    assert call(env, "read_file", path="s", offset="soon") == "abc"


def test_read_huge_file_is_bounded(env, monkeypatch):
    f = env.ws / "huge"
    f.write_bytes(b"y" * (5 * 1024 * 1024))
    reads = []
    real_open = Path.open
    def spy(self, *a, **k):
        fh = real_open(self, *a, **k)
        orig = fh.read
        def rd(n=-1):
            reads.append(n)
            return orig(n)
        fh.read = rd
        return fh
    monkeypatch.setattr(Path, "open", spy)
    out = call(env, "read_file", path="huge")
    assert "truncated" in out
    assert all(0 < n <= 200_000 for n in reads), reads  # never read(-1) on a 5 MB file


def test_read_utf8_multibyte_not_mangled_at_window_edge(env):
    (env.ws / "u").write_text("é" * 20000)
    out = call(env, "read_file", path="u")
    assert out.startswith("é" * 12000) and "\ufffd" not in out


def test_read_invalid_utf8_replaced_not_error(env):
    (env.ws / "lat").write_bytes(b"caf\xe9 ok")
    assert call(env, "read_file", path="lat") == "caf\ufffd ok"


# --- write_file -----------------------------------------------------------------------------------

def test_write_dict_content_is_json(env):
    assert check(env, "write_file", path="c.json", content={"a": 1, "b": [1, 2]}) is None
    call(env, "write_file", path="c.json", content={"a": 1, "b": [1, 2]})
    assert json.loads((env.ws / "c.json").read_text()) == {"a": 1, "b": [1, 2]}


def test_write_list_content_is_json(env):
    call(env, "write_file", path="l.json", content=[1, "é"])
    assert json.loads((env.ws / "l.json").read_text(encoding="utf-8")) == [1, "é"]


def test_write_lone_surrogate_written_as_replacement(env):
    out = call(env, "write_file", path="s.txt", content="a\udcffb")
    assert out.startswith("wrote s.txt")
    assert (env.ws / "s.txt").read_text(encoding="utf-8") == "a\ufffdb"


@pytest.mark.parametrize("p", ["", "  ", None, "sub/", "sub\\"])
def test_write_needs_a_file_path(env, p):
    out = call(env, "write_file", path=p, content="x")
    assert out.startswith("ERROR: write_file needs a file path")


def test_write_to_existing_directory_refused(env):
    (env.ws / "d").mkdir()
    assert call(env, "write_file", path="d", content="x").startswith("ERROR: d is a directory")
    assert (env.ws / "d").is_dir()


def test_write_is_atomic_on_failure(env, monkeypatch):
    (env.ws / "keep.txt").write_text("original")
    import os
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    out = call(env, "write_file", path="keep.txt", content="new")
    assert out.startswith("ERROR") and "No space" in out
    assert (env.ws / "keep.txt").read_text() == "original"
    assert [p.name for p in env.ws.iterdir() if p.name.endswith(".tmp")] == []


def test_write_keeps_crlf_and_utf8(env):
    call(env, "write_file", path="w.txt", content="a\r\nb 😀")
    assert (env.ws / "w.txt").read_bytes() == "a\r\nb 😀".encode("utf-8")


@pytest.mark.parametrize("raw,want", [(None, ""), (5, "5"), (b"by", "by"), ({"k": "é"}, '{\n  "k": "é"\n}\n')])
def test_write_content_coercion(raw, want):
    assert write_content(raw) == want


# --- fetch ---------------------------------------------------------------------------------------

@pytest.fixture()
def net(monkeypatch):
    def use(handler):
        monkeypatch.setattr(tools, "FETCH_TRANSPORT", httpx.MockTransport(handler))
    yield use


@pytest.mark.parametrize("raw", ["<https://ex.com/a>", ' "https://ex.com/a" ', "`https://ex.com/a`", "https://ex.com/a\n"])
def test_clean_url(raw):
    assert clean_url(raw) == "https://ex.com/a"


def test_fetch_wrapped_url_works(env, net):
    net(lambda r: httpx.Response(200, text="<p>ok</p>", headers={"content-type": "text/html"}))
    assert call(env, "fetch", url="<https://ex.com/a>").startswith("[HTTP 200]")


def test_fetch_uppercase_scheme(env, net):
    net(lambda r: httpx.Response(200, text="ok"))
    assert "ok" in call(env, "fetch", url="HTTPS://ex.com")


def test_fetch_unescapes_entities(env, net):
    net(lambda r: httpx.Response(200, text="<b>Tom &amp; Jerry &lt;3 &#x27;x&#x27;</b>",
                                 headers={"content-type": "text/html; charset=utf-8"}))
    assert "Tom & Jerry <3 'x'" in call(env, "fetch", url="https://ex.com")


def test_fetch_binary_summarised(env, net):
    net(lambda r: httpx.Response(200, content=b"%PDF-1.7\x00\x01" * 50, headers={"content-type": "application/pdf"}))
    out = call(env, "fetch", url="https://ex.com/f.pdf")
    assert out.startswith("[HTTP 200] binary content (application/pdf, 500 bytes)") and "\x00" not in out


def test_fetch_nul_body_with_text_type_summarised(env, net):
    net(lambda r: httpx.Response(200, content=b"\x00\x00\x00", headers={"content-type": "text/plain"}))
    assert "binary content" in call(env, "fetch", url="https://ex.com")


def test_fetch_body_is_capped(env, net, monkeypatch):
    monkeypatch.setattr(tools, "FETCH_MAX_BYTES", 1000)
    net(lambda r: httpx.Response(200, content=b"z" * 50000, headers={"content-type": "text/plain"}))
    out = call(env, "fetch", url="https://ex.com")
    assert out.endswith("[body cut at 2 MB]") and out.count("z") == 1000


def test_fetch_timeout_message(env, net):
    def h(r):
        raise httpx.ReadTimeout("slow", request=r)
    net(h)
    assert call(env, "fetch", url="https://ex.com").startswith("ERROR: fetch timed out after 30s")


def test_fetch_connect_error_message(env, net):
    def h(r):
        raise httpx.ConnectError("refused", request=r)
    net(h)
    assert call(env, "fetch", url="https://ex.com").startswith("ERROR: fetch failed (ConnectError)")


def test_fetch_bad_charset_falls_back(env, net):
    net(lambda r: httpx.Response(200, content="héllo".encode(), headers={"content-type": "text/plain; charset=bogus-9"}))
    assert "héllo" in call(env, "fetch", url="https://ex.com")


@pytest.mark.parametrize("u", ["ftp://x", "file:///etc/passwd", "javascript:alert(1)", "", None])
def test_fetch_non_http_refused(env, u):
    assert call(env, "fetch", url=u) == "ERROR: url must start with http(s)://"


def test_fetch_respects_network_off(env, net):
    env.s.allow_network = False
    assert call(env, "fetch", url="https://ex.com") == "ERROR: network access is disabled."
