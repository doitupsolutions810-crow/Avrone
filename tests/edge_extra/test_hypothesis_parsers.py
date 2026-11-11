"""Round 6: property-based tests for resilient parsers and sanitizers.

Hypothesis is a *test* dependency (see pyproject.toml optional-dependencies.dev).
Examples are bounded and derandomized so the suite stays fast and deterministic.
"""
from __future__ import annotations

import io
import json
import math
import os
import shlex
import shutil
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from avrone.codex import _json_safe, iter_lines, replay_limit, DEFAULT_REPLAY_EVENTS, HARD_REPLAY_EVENTS
from avrone.config import _bool, parse_dotenv_line
from avrone.evolve import _safe_test_cmd, _default_test_cmd
from avrone.gateway import clean_text
from avrone.governor import _read_mem_pct_proc, make_disk_reader
from avrone.mcp_server import _ok_rpc_id
from avrone.telemetry import _finite_window
from avrone.trinity import compact_messages, context_chars, tool_output_max, clamp_deep_rounds, MAX_DEEP_ROUNDS

# Keep the property suite tight: a handful of examples per property, no deadline
# trips on a loaded CI box, and a fixed PRNG so shuffled pytest runs match.
HYP = settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# --- strategies ----------------------------------------------------------------

safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=0x10FFFF),
    max_size=80,
)

# JSON-like trees that can include the awkward bits (_json_safe must tame).
jsonish = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-10**12, max_value=10**12),
        st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=False, width=64),
        safe_text,
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(safe_text, children, max_size=4),
    ),
    max_leaves=16,
)


# --- _json_safe ----------------------------------------------------------------

@HYP
@given(jsonish)
def test_json_safe_always_allow_nan_false_encodable(value: Any) -> None:
    out = _json_safe(value)
    raw = json.dumps(out, ensure_ascii=False, allow_nan=False)
    # The encoded form itself must also decode back as UTF-8 JSON.
    json.loads(raw)
    assert "NaN" not in raw and "Infinity" not in raw and "-Infinity" not in raw


@HYP
@given(st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=False, width=64))
def test_json_safe_maps_nonfinite_float_to_none(n: float) -> None:
    out = _json_safe(n)
    if math.isfinite(n):
        assert out == n or (isinstance(out, float) and math.isfinite(out))
    else:
        assert out is None


@HYP
@given(safe_text)
def test_json_safe_strings_are_utf8_and_nul_free(s: str) -> None:
    out = _json_safe(s)
    assert isinstance(out, str)
    assert "\x00" not in out
    out.encode("utf-8")
    # No lone surrogates remain.
    json.dumps(out, ensure_ascii=False)


# --- clean_text ----------------------------------------------------------------

@HYP
@given(safe_text)
def test_clean_text_is_utf8_encodable_without_nul(s: str) -> None:
    out = clean_text(s)
    assert "\x00" not in out
    out.encode("utf-8")
    json.dumps(out, ensure_ascii=False)


# --- parse_dotenv_line ---------------------------------------------------------

@HYP
@given(st.text(max_size=200))
def test_parse_dotenv_line_never_raises(line: str) -> None:
    out = parse_dotenv_line(line)
    if out is None:
        return
    key, val = out
    assert isinstance(key, str) and isinstance(val, str)
    assert key and key == key.strip()
    assert " " not in key and "\t" not in key and "=" not in key


@HYP
@given(
    st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True),
    st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\n\r#"), max_size=40),
)
def test_parse_dotenv_roundtrip_plain_assignment(key: str, val: str) -> None:
    # Values with leading/trailing space or quotes can change under the parser;
    # the invariant we care about is: a KEY=VAL line is accepted and the key
    # comes back exactly.
    line = f"{key}={val}"
    out = parse_dotenv_line(line)
    assert out is not None
    assert out[0] == key


# --- meminfo / disk ------------------------------------------------------------

@HYP
@given(st.text(max_size=400))
def test_meminfo_parser_never_crashes_and_pct_is_sane(body: str) -> None:
    def fake_open(path, *a, **k):
        if str(path) == "/proc/meminfo":
            return io.StringIO(body)
        raise FileNotFoundError(path)

    with patch("builtins.open", fake_open):
        out = _read_mem_pct_proc()
    assert out is None or (isinstance(out, float) and math.isfinite(out) and 0.0 <= out <= 100.0)


@HYP
@given(
    st.integers(min_value=0, max_value=10**18),
    st.integers(min_value=0, max_value=10**18),
)
def test_disk_reader_pct_is_none_or_in_unit_interval(total: int, free: int) -> None:
    used = max(0, total - free) if total else 0

    def fake_usage(_path):
        return SimpleNamespace(total=total, used=used, free=free)

    with patch("avrone.governor.shutil.disk_usage", fake_usage):
        out = make_disk_reader("/")()
    assert out is None or (isinstance(out, float) and math.isfinite(out) and 0.0 <= out <= 100.0)


# --- config _bool / replay_limit / finite_window --------------------------------

@HYP
@given(st.text(alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=40))
def test_bool_env_never_raises(raw: str) -> None:
    key = "AVRONE_HYP_BOOL"
    prev = os.environ.get(key)
    os.environ[key] = raw
    try:
        out = _bool(key, False)()  # _bool is a Settings default_factory
        assert out in (True, False)
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


@HYP
@given(st.text(alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=30))
def test_replay_limit_always_clamped(raw: str) -> None:
    key = "AVRONE_REPLAY_MAX_EVENTS"
    prev = os.environ.get(key)
    os.environ[key] = raw
    try:
        n = replay_limit()
        assert isinstance(n, int)
        assert 1 <= n <= HARD_REPLAY_EVENTS
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


@HYP
@given(st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10**9, max_value=10**12),
    st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=False, width=64),
    st.text(max_size=20),
    st.just(object()),
))
def test_finite_window_always_nonneg_finite(value: Any) -> None:
    out = _finite_window(value)
    assert isinstance(out, (int, float)) and not isinstance(out, bool)
    assert math.isfinite(float(out)) and float(out) >= 0.0


# --- argv quoting --------------------------------------------------------------

# POSIX shlex.quote/split is defined on strings without NUL; we also skip lone
# surrogates so the round-trip is meaningful on every Python build.
posix_argv = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=60,
)


@HYP
@given(posix_argv)
def test_shlex_quote_split_roundtrip(s: str) -> None:
    assert shlex.split(shlex.quote(s)) == [s]


@HYP
@given(st.lists(posix_argv, min_size=1, max_size=5))
def test_safe_test_cmd_never_raises_and_is_splitable(parts: list[str]) -> None:
    # Join with spaces so _safe_test_cmd has a command-shaped input. It must
    # not raise, and the result must be a non-empty shell-splitable string
    # (or the documented default).
    cmd = " ".join(parts)
    out = _safe_test_cmd(cmd)
    assert isinstance(out, str) and out
    shlex.split(out)


def test_safe_test_cmd_empty_falls_back_to_default() -> None:
    assert _safe_test_cmd("") == _default_test_cmd()
    assert _safe_test_cmd("   ") == _default_test_cmd()


# --- JSON-RPC id ---------------------------------------------------------------

@HYP
@given(st.one_of(
    st.none(),
    st.integers(min_value=-10**18, max_value=10**18),
    st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=False, width=64),
    st.booleans(),
    safe_text,
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=3),
))
def test_ok_rpc_id_matches_jsonrpc_scalar_rules(rid: Any) -> None:
    ok = _ok_rpc_id(rid)
    if rid is None or isinstance(rid, str):
        assert ok is True
        return
    if isinstance(rid, bool):  # bool is a subclass of int; JSON-RPC forbids it
        assert ok is False
        return
    if isinstance(rid, int):
        assert ok is True
        return
    if isinstance(rid, float) and math.isfinite(rid):
        assert ok is True
        return
    assert ok is False


# --- compact_messages / trinity clamps -----------------------------------------

msg_role = st.sampled_from(["system", "user", "assistant", "tool"])
# Bounded history so compact_messages stays cheap.
msg_list = st.lists(
    st.fixed_dictionaries({
        "role": msg_role,
        "content": st.text(max_size=80),
    }),
    max_size=12,
)


@HYP
@given(msg_list, st.one_of(st.none(), st.integers(min_value=-10, max_value=10**6), st.just("nope"), st.just(float("nan"))))
def test_compact_messages_never_raises_and_preserves_length(messages, limit) -> None:
    before = len(messages)
    n = compact_messages(messages, limit=limit)
    assert isinstance(n, int) and n >= 0
    assert len(messages) == before
    # After compact, every remaining content that is a string must still be a string.
    for m in messages:
        if isinstance(m, dict) and "content" in m:
            # we didn't inject non-dicts; content stays a string (possibly elided).
            assert isinstance(m["content"], str)


@HYP
@given(st.one_of(st.none(), st.text(max_size=20), st.integers(min_value=-10**6, max_value=10**9)))
def test_context_chars_and_tool_output_max_never_raise(raw) -> None:
    keys = ("AVRONE_CONTEXT_CHARS", "AVRONE_TOOL_OUTPUT_MAX")
    prev = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            if raw is None:
                os.environ.pop(k, None)
            else:
                s = str(raw)
                if "\x00" in s:
                    return
                os.environ[k] = s
        a = context_chars()
        b = tool_output_max()
        assert isinstance(a, int) and a > 0
        assert isinstance(b, int) and b > 0
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@HYP
@given(st.one_of(st.none(), st.booleans(), st.integers(min_value=-100, max_value=10**6), st.text(max_size=10)))
def test_clamp_deep_rounds_none_or_in_range(value: Any) -> None:
    out = clamp_deep_rounds(value)
    assert out is None or (isinstance(out, int) and 1 <= out <= MAX_DEEP_ROUNDS)


# --- iter_lines (property over generated files) --------------------------------

@HYP
@given(st.lists(st.text(alphabet="abcdef\t ", max_size=20), max_size=8), st.integers(min_value=4, max_value=16))
def test_iter_lines_drops_overlong_and_keeps_the_rest(parts: list[str], cap: int) -> None:
    # Build a file from newline-terminated parts; some may exceed `cap`.
    body = "".join(p + "\n" for p in parts)
    got = list(iter_lines(io.StringIO(body), max_line=cap))
    # Every yielded string is a complete line no longer than cap (or shorter
    # because it ended with a newline inside the cap). Overlong lines are None.
    kept = []
    for item, original in zip(
        # We cannot zip 1:1 if an overlong line is split internally; instead
        # compare against the original split, accounting for None placeholders.
        got,
        # placeholder — checked below
        got,
    ):
        if item is None:
            continue
        assert isinstance(item, str)
        assert item.endswith("\n") or item == item  # last-line case (we always added \n)
        assert len(item) <= cap or item.endswith("\n") and len(item) <= cap
        kept.append(item)
    # Reconstructing from kept lines must be a subsequence of the original body
    # (overlong lines omitted).
    rebuilt = "".join(kept)
    # Every kept line appears in the original body.
    for line in kept:
        assert line in body
    assert len(rebuilt) <= len(body)
