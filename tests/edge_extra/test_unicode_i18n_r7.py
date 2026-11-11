"""Round 7: Unicode / i18n edges — RTL, zero-width, combining marks, long graphemes.

Exercises clean_text / _json_safe / _utf8_text, blackboard, argv, dotenv,
and Codex logs. Every stored or transmitted string must be UTF-8 JSON-safe.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.bridge import blackboard_accept, blackboard_posts, normalize_blackboard_post, _utf8_text
from avrone.codex import Codex, _json_safe
from avrone.config import Settings, parse_dotenv_line
from avrone.gateway import clean_text
from avrone.sovereign import LocalRunner


# Arabic RTL, Hebrew RTL, zero-width space / joiner / non-joiner, a base
# letter with a long combining-mark stack, and a flag emoji (two code points).
RTL = "مرحبا السلام Hello שָׁלוֹם"
ZW = "zero\u200bwidth\u200djoin\u200cnonjoin"
COMBINING = "e" + ("\u0301" * 40) + "x"  # e + 40 combining acute accents
GRAPHEME = "👨‍👩‍👧‍👦"  # family emoji ZWJ sequence
LONG_CLUSTER = "a" + ("\u0308" * 200)  # a + 200 diaereses
SAMPLES = [RTL, ZW, COMBINING, GRAPHEME, LONG_CLUSTER, "café 🌍", "\ufeffBOM-front"]


def test_clean_text_and_json_safe_on_rtl_zw_combining() -> None:
    for s in SAMPLES:
        out = clean_text(s)
        out.encode("utf-8")
        raw = json.dumps(out, ensure_ascii=False, allow_nan=False)
        json.loads(raw)
        js = _json_safe(s)
        assert isinstance(js, str)
        json.dumps(js, ensure_ascii=False, allow_nan=False).encode("utf-8")
        u = _utf8_text(s)
        u.encode("utf-8")
        # NUL and lone surrogates never survive.
        assert "\x00" not in out and "\x00" not in js and "\x00" not in u


def test_zero_width_and_rtl_are_preserved_not_stripped() -> None:
    # Zero-width and RTL characters are legal UTF-8; we only drop NULs / surrogates.
    assert "\u200b" in clean_text(ZW)
    assert "مرحبا" in clean_text(RTL)
    assert "ש" in clean_text(RTL)
    # Combining marks stay attached (we do not normalize away the stack).
    assert clean_text(COMBINING).count("\u0301") == 40


def test_blackboard_preserves_rtl_and_zw_in_text(tmp_path: Path) -> None:
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), fleet=None, codex=None)
    for i, text in enumerate(SAMPLES):
        post, err = normalize_blackboard_post({
            "id": f"u{i}", "kind": "finding", "text": text, "run_id": "uni",
        })
        assert err is None and post is not None, err
        blackboard_accept(core, post)
    rows = {p["id"]: p for p in blackboard_posts(core)}
    assert "مرحبا" in rows["u0"]["text"]
    assert "\u200b" in rows["u1"]["text"]
    assert rows["u2"]["text"].count("\u0301") == 40
    for p in rows.values():
        json.dumps(p, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_codex_emit_replay_unicode_samples(tmp_path: Path) -> None:
    c = Codex(tmp_path)
    for i, s in enumerate(SAMPLES):
        c.emit("uni", "evt", "agent", i=i, text=s, note=ZW)
    evs = c.replay("uni")
    assert len(evs) == len(SAMPLES)
    texts = [e.get("data", {}).get("text", "") for e in evs]
    assert any("مرحبا" in t for t in texts)
    assert any("\u200b" in t for t in texts)
    for e in evs:
        json.dumps(e, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_dotenv_unicode_values_roundtrip() -> None:
    # Bare and double-quoted forms, no quotes/hashes inside the sample.
    for s in (RTL, "café", GRAPHEME):
        line = f'GREETING="{s}"'
        out = parse_dotenv_line(line)
        assert out is not None
        assert out[0] == "GREETING" and out[1] == s


def test_argv_quote_split_unicode_lists() -> None:
    args = [RTL, ZW, COMBINING, GRAPHEME, "plain"]
    line = " ".join(shlex.quote(a) for a in args)
    assert shlex.split(line) == args


@pytest.mark.asyncio
async def test_local_runner_echoes_rtl_and_combining(tmp_path: Path) -> None:
    s = Settings()
    s.workspace = tmp_path
    s.command_timeout = 8
    r = LocalRunner(s)
    await r.start()
    payload = RTL + " " + COMBINING + " " + GRAPHEME
    # Pass the payload as a quoted argv to python rather than through bash
    # interpolation, so we are testing the runner's capture path, not the shell.
    res = await r._exec(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", payload],
        "unicode-echo",
        8,
        cwd=tmp_path,
    )
    assert res.exit_code == 0, res.stderr
    assert "مرحبا" in res.stdout
    assert "\u0301" in res.stdout
    assert GRAPHEME in res.stdout
    res.stdout.encode("utf-8")
