"""Round 7: stateful / round-trip hypothesis properties.

JSON-RPC encode→decode→encode, dotenv serialize→load, blackboard write→read,
argv list quote→split, and a bounded RuleBasedStateMachine for an MCP
client/server session. Derandomized and capped so the suite stays fast.
"""
from __future__ import annotations

import asyncio
import io
import json
import shlex
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hypothesis import HealthCheck, given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test

from avrone import mcp_server as ms
from avrone.bridge import (
    KINDS,
    blackboard_accept,
    blackboard_posts,
    normalize_blackboard_post,
    public_post,
    safe_since,
    _utf8_text,
)
from avrone.config import Settings, _num, parse_dotenv_line
from avrone.mcp_server import _ok_rpc_id
from avrone.gateway import clean_text


HYP = settings(
    max_examples=35,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

STATE = settings(
    max_examples=12,
    stateful_step_count=10,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# --- JSON-RPC encode → decode → encode ---------------------------------------

rpc_id = st.one_of(
    st.none(),
    st.integers(min_value=-10**12, max_value=10**12),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=24),
)
rpc_method = st.from_regex(r"[A-Za-z][A-Za-z0-9_/]{0,24}", fullmatch=True)
# JSON-safe trees (finite numbers only — NaN/Inf are not legal JSON).
json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10**9, max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False, allow_subnormal=False, width=64),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40),
)
json_val = st.recursive(
    json_scalar,
    lambda c: st.one_of(st.lists(c, max_size=3), st.dictionaries(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=12),
        c, max_size=3,
    )),
    max_leaves=8,
)


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)


@HYP
@given(rpc_id, rpc_method, st.one_of(st.none(), json_val))
def test_jsonrpc_request_encode_decode_encode_stable(rid, method, params) -> None:
    # bool is a legal JSON value but not a legal JSON-RPC id; skip it (strategy
    # already excludes bool-as-int... actually booleans aren't in rpc_id).
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    raw = _canon(msg)
    back = json.loads(raw)
    assert _canon(back) == raw
    assert _ok_rpc_id(back["id"]) is True
    # The decoded request is still a well-formed JSON-RPC request object.
    assert back["jsonrpc"] == "2.0" and back["method"] == method


@HYP
@given(json_val)
def test_json_value_dumps_loads_dumps_stable(value: Any) -> None:
    raw = _canon(value)
    assert _canon(json.loads(raw)) == raw


# --- dotenv serialize → load --------------------------------------------------

dotenv_key = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True)
# Values that our serializer can emit without quoting pitfalls: no quotes,
# comments, NULs, or newlines (parse_dotenv_line does not unescape).
dotenv_val = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\x00#'\"\n\r"),
    max_size=40,
).filter(lambda s: s == s.strip())


@HYP
@given(dotenv_key, dotenv_val)
def test_dotenv_serialize_load_roundtrip(key: str, val: str) -> None:
    # Double-quoted form, and the bare KEY=VAL form when it is unambiguous.
    quoted = f'{key}="{val}"'
    out = parse_dotenv_line(quoted)
    assert out == (key, val)
    if val and not val.startswith((" ", "\t")) and " #" not in val and "\t#" not in val:
        bare = parse_dotenv_line(f"{key}={val}")
        assert bare == (key, val)


@HYP
@given(st.sampled_from(["AVRONE_SQUAD_SIZE", "AVRONE_MAX_STEPS", "AVRONE_MAX_PARALLEL"]),
       st.integers(min_value=1, max_value=64))
def test_settings_int_env_roundtrip(name: str, n: int) -> None:
    import os
    prev = os.environ.get(name)
    try:
        os.environ[name] = str(n)
        a = Settings()
        field = {"AVRONE_SQUAD_SIZE": "squad_size", "AVRONE_MAX_STEPS": "max_steps",
                 "AVRONE_MAX_PARALLEL": "max_parallel"}[name]
        assert getattr(a, field) == n
        os.environ[name] = str(getattr(a, field))
        b = Settings()
        assert getattr(b, field) == n
    finally:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


@HYP
@given(st.text(alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=24),
       st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=False, width=64))
def test_num_never_returns_nonfinite(raw: str, default: float) -> None:
    # _num takes a string raw; also feed the default through a second call.
    if not (default == default and abs(default) != float("inf")):
        default = 1.0
    out = _num(raw, default, float)
    assert isinstance(out, (int, float)) and not isinstance(out, bool)
    import math
    assert math.isfinite(float(out))


# --- argv list quote → split --------------------------------------------------

posix_arg = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=30,
)


@HYP
@given(st.lists(posix_arg, min_size=1, max_size=6))
def test_argv_list_quote_split_roundtrip(args: list[str]) -> None:
    line = " ".join(shlex.quote(a) for a in args)
    assert shlex.split(line) == args


# --- blackboard write → read fidelity ----------------------------------------

safe_id = st.from_regex(r"[A-Za-z][A-Za-z0-9_.:-]{0,20}", fullmatch=True)
safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=80,
).filter(lambda s: clean_text(s).strip() != "")


@HYP
@given(safe_id, st.sampled_from(sorted(KINDS)), safe_text, safe_id)
def test_blackboard_write_read_fidelity(pid: str, kind: str, text: str, run_id: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        core = SimpleNamespace(
            settings=SimpleNamespace(data_dir=Path(d)),
            fleet=None,
            codex=None,
        )
        post, err = normalize_blackboard_post({
            "id": pid, "kind": kind, "text": text, "run_id": run_id,
            "agent": "writer", "origin_node": "local",
        })
        assert err is None and post is not None
        dup, stored_id = blackboard_accept(core, post)
        assert dup is False and stored_id == pid
        # A second accept of the same id is a duplicate, not a rewrite.
        dup2, stored2 = blackboard_accept(core, post)
        assert dup2 is True and stored2 == pid
        got = blackboard_posts(core)
        assert len(got) == 1
        row = got[0]
        assert row["id"] == pid
        assert row["kind"] == kind
        # Sealed / public form is UTF-8 JSON-encodable and matches the written text
        # after the same sanitizer the production path applies.
        assert row["text"] == public_post(post)["text"]
        json.dumps(row, allow_nan=False, ensure_ascii=False).encode("utf-8")
        # Cursor: since=this id yields nothing newer.
        assert blackboard_posts(core, since=safe_since(pid)) == []


# --- MCP session state machine ------------------------------------------------

class _FakeTools:
    def list(self):
        return [{"name": "echo", "source": "builtin", "args": "{}", "description": "echo"}]


class _FakeCore:
    def __init__(self):
        self.tools = _FakeTools()

    async def squad(self, task, size=None):
        return {"answer": "ok", "run_id": "r1", "agents": [1]}

    async def chat(self, task):
        return "ok"

    async def deep(self, task, first=None):
        return {"answer": "deep", "run_id": "r2"}


async def _run_session(ops: list[Any]) -> list[dict[str, Any]]:
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    for op in ops:
        if isinstance(op, (bytes, bytearray)):
            reader.feed_data(bytes(op) if op.endswith(b"\n") else bytes(op) + b"\n")
        else:
            reader.feed_data((json.dumps(op, allow_nan=False) + "\n").encode())
    reader.feed_eof()
    out = io.BytesIO()

    async def factory():
        return _FakeCore(), {}

    await asyncio.wait_for(ms.AvroneMCPServer(core_factory=factory).serve(reader, out), 3)
    msgs = []
    for line in out.getvalue().decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0"
        assert "result" in msg or "error" in msg
        if "id" in msg:
            assert _ok_rpc_id(msg["id"]) is True
        msgs.append(msg)
    return msgs


class MCPSessionMachine(RuleBasedStateMachine):
    """A bounded MCP stdio session: mix of requests, notifications, and junk."""

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[Any] = []
        self.pending: list[Any] = []  # ids of well-formed requests we expect answered
        self._seen: set[str] = set()

    def _uid(self, rid: Any) -> str:
        return json.dumps(rid, allow_nan=False)

    @rule(rid=st.integers(min_value=0, max_value=10**6),
          method=st.sampled_from(["ping", "tools/list", "initialize", "nope"]))
    def request(self, rid: int, method: str) -> None:
        key = self._uid(rid)
        if key in self._seen:
            return
        self._seen.add(key)
        self.ops.append({"jsonrpc": "2.0", "id": rid, "method": method})
        self.pending.append(rid)

    @rule(rid=st.text(alphabet="abcABC012", min_size=0, max_size=8),
          method=st.sampled_from(["ping", "tools/list"]))
    def request_string_id(self, rid: str, method: str) -> None:
        key = self._uid(rid)
        if key in self._seen:
            return
        self._seen.add(key)
        self.ops.append({"jsonrpc": "2.0", "id": rid, "method": method})
        self.pending.append(rid)

    @rule()
    def notify(self) -> None:
        self.ops.append({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @rule()
    def junk_line(self) -> None:
        self.ops.append(b"not-json\n")

    @rule()
    def bad_version(self) -> None:
        # Distinct id so we can still check the error is well-formed.
        rid = f"badver-{len(self.ops)}"
        self.ops.append({"jsonrpc": "1.0", "id": rid, "method": "ping"})
        self.pending.append(rid)

    def teardown(self) -> None:
        msgs = asyncio.run(_run_session(self.ops))
        by_id: dict[str, int] = {}
        for m in msgs:
            if "id" in m and m["id"] is not None:
                k = self._uid(m["id"])
                by_id[k] = by_id.get(k, 0) + 1
        for rid in self.pending:
            assert by_id.get(self._uid(rid), 0) == 1, (rid, by_id, msgs)


def test_mcp_session_state_machine() -> None:
    run_state_machine_as_test(MCPSessionMachine, settings=STATE)
