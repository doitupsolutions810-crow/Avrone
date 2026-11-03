"""Round 34: Eternal CodeX event log and the HUD's live stream (/ws/events). Covers event data
that isn't plain JSON (tuple keys, NaN, bytes, sets, cycles, hostile __str__), run ids that aren't
plain names, a slow dashboard learning it missed events, and a dashboard that stops reading."""
import asyncio
import json
import math
import time

import pytest
from fastapi.testclient import TestClient

from avrone import codex as cx_mod
from avrone import server
from avrone.codex import Codex, dumps_event, jsonable, log_name


@pytest.fixture()
def cx(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_CODEX_REDACT", raising=False)
    return Codex(tmp_path / "data")


def lines(cx, rid="r1"):
    return [json.loads(x, parse_constant=lambda c: pytest.fail(f"non-strict JSON {c}"))
            for x in (cx.log_dir / f"{log_name(rid)}.jsonl").read_text().splitlines()]


# ------------------------------------------------------------------ jsonable
@pytest.mark.parametrize("v,want", [
    (float("nan"), None), (float("inf"), None), (float("-inf"), None), (1.5, 1.5), (0, 0), (True, True),
    (None, None), ("s", "s"), (b"hi", "hi"), (b"\xff", "\ufffd"), (bytearray(b"ab"), "ab"),
    ((1, 2), [1, 2]), ({3, 1, 2}, [1, 2, 3]), (frozenset(), []),
])
def test_jsonable_scalars(v, want):
    assert jsonable(v) == want


def test_jsonable_keys():
    out = jsonable({(1, 2): "t", 1: "i", None: "n", 2.5: "f", float("nan"): "x"})
    assert out == {"(1, 2)": "t", "1": "i", "null": "n", "2.5": "f", "nan": "x"}
    json.dumps(out, allow_nan=False)


def test_jsonable_cycle_bounded():
    a = []
    a.append(a)
    out = jsonable(a)
    json.dumps(out, allow_nan=False)
    depth = 0
    while isinstance(out, list):
        out, depth = out[0], depth + 1
    assert out == "[...]" and depth == cx_mod.JSON_MAX_DEPTH


def test_jsonable_bool_key():
    assert jsonable({True: 1, False: 0}) == {"true": 1, "false": 0}


def test_jsonable_hostile_str():
    class Evil:
        def __str__(self):
            raise RuntimeError("nope")
    assert jsonable(Evil()) == "<Evil>"
    assert jsonable({Evil(): 1}) == {"<Evil>": 1}


def test_jsonable_nested_nan():
    assert jsonable({"a": [1.0, {"b": float("nan")}]}) == {"a": [1.0, {"b": None}]}


def test_dumps_event_strict():
    s = dumps_event({"x": float("nan"), "y": {1, 2}})
    assert "NaN" not in s and json.loads(s) == {"x": None, "y": [1, 2]}


def test_dumps_event_clean_unchanged():
    ev = {"a": 1, "b": "c"}
    assert dumps_event(ev) == json.dumps(ev)


# ------------------------------------------------------------------ emit never raises on odd data
@pytest.mark.parametrize("redact", ["on", "off"])
def test_emit_tuple_key(cx, monkeypatch, redact):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", redact)
    ev = cx.emit("r1", "tool.result", "A", grid={(0, 1): "x"})
    assert ev.data == {"grid": {"(0, 1)": "x"}}
    assert lines(cx)[0]["data"]["grid"] == {"(0, 1)": "x"}


@pytest.mark.parametrize("redact", ["on", "off"])
def test_emit_nan_is_strict(cx, monkeypatch, redact):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", redact)
    cx.emit("r1", "t", "A", score=float("nan"), hi=float("inf"))
    assert "NaN" not in (cx.log_dir / "r1.jsonl").read_text()
    assert lines(cx)[0]["data"] == {"score": None, "hi": None}


def test_emit_cycle_redact_off(cx, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "off")
    a = {}
    a["self"] = a
    cx.emit("r1", "t", "A", loop=a)  # used to raise ValueError: Circular reference detected
    assert lines(cx)[0]["kind"] == "t"


def test_emit_bytes_and_set_live_safe(cx):
    ev = cx.emit("r1", "t", "A", b=b"raw", s={"x"})
    json.dumps(ev.data, allow_nan=False)  # what the live stream sends
    assert ev.data == {"b": "raw", "s": ["x"]}


def test_emit_non_str_kind_agent(cx):
    ev = cx.emit("r1", 5, None)
    assert ev.kind == "5" and ev.agent == "None"


def test_emit_still_redacts(cx, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "on")
    tok = "ghp_" + "A" * 36
    cx.emit("r1", "t", "A", out=f"token {tok}")
    assert tok not in (cx.log_dir / "r1.jsonl").read_text()


# ------------------------------------------------------------------ replay of logs
def test_replay_old_nan_log_is_servable(cx):
    (cx.log_dir / "old.jsonl").write_text('{"run_id":"old","kind":"t","agent":"A","data":{"x":NaN,"y":Infinity},"ts":1,"id":"a"}\n')
    ev = cx.replay("old")
    assert ev[0]["data"] == {"x": None, "y": None}
    json.dumps(ev, allow_nan=False)


def test_replay_string_nan_untouched(cx):
    cx.emit("r1", "t", "A", word="NaN Infinity")
    assert cx.replay("r1")[0]["data"]["word"] == "NaN Infinity"


# ------------------------------------------------------------------ run id -> log file
@pytest.mark.parametrize("rid", ["../escape", "a/b", "/abs", ".", "..", ".hidden", "x" * 129, "sp ace", "", "é", "a\x00b"])
def test_odd_run_ids_stay_in_runs_dir(cx, tmp_path, rid):
    cx.emit(rid, "t", "A", n=1)
    files = [p for p in tmp_path.rglob("*.jsonl")]
    assert files and all(p.parent == cx.log_dir for p in files)
    assert files[0].name.startswith("x-")
    assert cx.replay(rid)[0]["run_id"] == rid  # emit and replay agree


@pytest.mark.parametrize("rid", ["run-1", "msg-ab12", "a.b_c", "x" * 128])
def test_plain_run_ids_unchanged(cx, rid):
    assert log_name(rid) == rid
    cx.emit(rid, "t", "A")
    assert (cx.log_dir / f"{rid}.jsonl").exists()


def test_log_name_distinct_and_stable():
    assert log_name("../a") == log_name("../a") != log_name("../b")


def test_traversal_never_writes_outside(cx, tmp_path):
    cx.emit("../../pwn", "t", "A")
    assert not (tmp_path / "pwn.jsonl").exists() and not (tmp_path.parent / "pwn.jsonl").exists()


# ------------------------------------------------------------------ slow subscriber gap notice
def test_gap_notice_after_overflow(cx):
    async def go():
        q = cx.subscribe()
        for i in range(1005):
            cx.emit("r1", "t", "A", i=i)
        assert q.qsize() == 1000 and cx.dropped == 5
        while not q.empty():
            q.get_nowait()
        cx.emit("r1", "t", "A", i="after")
        gap = q.get_nowait()
        nxt = q.get_nowait()
        assert gap.kind == "stream.gap" and gap.data["missed"] == 5
        assert nxt.data["i"] == "after"
        cx.emit("r1", "t", "A", i="again")
        assert q.get_nowait().kind == "t"  # reported once
    asyncio.run(go())


def test_gap_waits_until_room_for_both(cx):
    async def go():
        q = cx.subscribe()
        for i in range(1001):
            cx.emit("r1", "t", "A", i=i)
        q.get_nowait()  # one slot free: not enough for notice + event, so the event goes in
        cx.emit("r1", "t", "A", i="x")
        assert q.qsize() == 1000 and cx._gaps[q] == 1
        q.get_nowait()
        q.get_nowait()  # two free now: the notice goes first
        cx.emit("r1", "t", "A", i="y")
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert [e.kind for e in items[-2:]] == ["stream.gap", "t"] and items[-2].data["missed"] == 1
    asyncio.run(go())


def test_no_gap_without_overflow(cx):
    async def go():
        q = cx.subscribe()
        cx.emit("r1", "t", "A")
        cx.emit("r1", "t", "A")
        assert [q.get_nowait().kind for _ in range(2)] == ["t", "t"]
    asyncio.run(go())


def test_unsubscribe_clears_gap(cx):
    async def go():
        q = cx.subscribe()
        for i in range(1002):
            cx.emit("r1", "t", "A", i=i)
        cx.unsubscribe(q)
        assert q not in cx._gaps
        cx.emit("r1", "t", "A")
        assert q not in cx._gaps
    asyncio.run(go())


def test_gaps_are_per_subscriber(cx):
    async def go():
        slow = cx.subscribe()
        for i in range(1001):
            cx.emit("r1", "t", "A", i=i)
        fast = cx.subscribe()
        cx.emit("r1", "t", "A")
        assert fast.get_nowait().kind == "t" and fast not in cx._gaps
    asyncio.run(go())


# ------------------------------------------------------------------ ws_send_timeout
@pytest.mark.parametrize("raw,want", [("", 30.0), ("5", 5.0), ("0", 1.0), ("-3", 1.0), ("9999", 600.0),
                                      ("nan", 30.0), ("inf", 30.0), ("abc", 30.0), (" 12 ", 12.0)])
def test_ws_send_timeout_env(monkeypatch, raw, want):
    monkeypatch.setenv("AVRONE_WS_SEND_TIMEOUT", raw)
    assert server.ws_send_timeout() == want


# ------------------------------------------------------------------ live stream
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    with TestClient(server.app) as c:
        yield c


def _wait(pred, s=5.0):
    end = time.time() + s
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_ws_streams_odd_data_and_survives(client):
    codex = server.core().codex
    with client.websocket_connect("/ws/events") as ws:
        assert _wait(lambda: len(codex._subscribers) >= 1)
        codex.emit("r-ws", "t", "A", x=float("nan"), b=b"\x00", s={1})
        codex.emit("r-ws", "t2", "A")
        first = json.loads(ws.receive_text(), parse_constant=lambda c: pytest.fail(c))
        assert first["data"] == {"x": None, "b": "\x00", "s": [1]}
        assert ws.receive_json()["kind"] == "t2"


def test_ws_event_unserializable_after_emit_still_sent(client):
    """An Event pushed straight onto a queue (not via emit) with junk data still goes out."""
    codex = server.core().codex
    with client.websocket_connect("/ws/events") as ws:
        assert _wait(lambda: len(codex._subscribers) >= 1)
        q = next(iter(codex._subscribers))
        loop = codex._loops[q]
        ev = cx_mod.Event(run_id="r", kind="raw", agent="A", data={"k": {(1,): float("nan")}})
        loop.call_soon_threadsafe(q.put_nowait, ev)
        got = ws.receive_json()
        assert got["kind"] == "raw" and got["data"] == {"k": {"(1,)": None}}


def test_ws_stalled_client_is_dropped(client, monkeypatch):
    monkeypatch.setenv("AVRONE_WS_SEND_TIMEOUT", "1")
    from starlette.websockets import WebSocket

    async def stall(self, data):
        await asyncio.sleep(3600)
    monkeypatch.setattr(WebSocket, "send_text", stall)
    codex = server.core().codex
    base = len(codex._subscribers)
    with client.websocket_connect("/ws/events"):
        assert _wait(lambda: len(codex._subscribers) == base + 1)
        codex.emit("r-ws", "t", "A")
        assert _wait(lambda: len(codex._subscribers) == base, 6.0)  # slot freed, not held forever


def test_ws_normal_client_not_dropped(client, monkeypatch):
    monkeypatch.setenv("AVRONE_WS_SEND_TIMEOUT", "1")
    codex = server.core().codex
    with client.websocket_connect("/ws/events") as ws:
        assert _wait(lambda: len(codex._subscribers) >= 1)
        for i in range(3):
            codex.emit("r-ws", "t", "A", i=i)
            assert ws.receive_json()["data"]["i"] == i
            time.sleep(0.4)
        codex.emit("r-ws", "t", "A", i=9)
        assert ws.receive_json()["data"]["i"] == 9


def test_api_runs_serves_old_nan_log(client):
    codex = server.core().codex
    (codex.log_dir / "oldrun.jsonl").write_text('{"run_id":"oldrun","kind":"t","agent":"A","data":{"x":NaN},"ts":1,"id":"a"}\n')
    r = client.get("/api/runs/oldrun")
    assert r.status_code == 200 and r.json()[0]["data"]["x"] is None
