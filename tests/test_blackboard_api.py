"""Fusion blackboard: GET/POST /api/blackboard.

CONTRACT.md (GreatScale fusion bridge): admin token on both methods, a 4096-byte
cap that answers 413 before the token is checked, Avrone's five post kinds, id
dedup that reports duplicate:true and does not overwrite, and a positional
`since` cursor over the fan-in (same rule as Bridge.export).
"""
import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from avrone import server
from avrone.bridge import (
    KINDS,
    Bridge,
    BridgeError,
    blackboard_accept,
    blackboard_posts,
    for_core,
    normalize_blackboard_post,
)

AUTH = {"X-Admin-Token": "test-token"}
POST_KEYS = {"id", "run_id", "agent", "role", "kind", "text", "refs", "ts", "origin_node"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def _post(client, body, headers=None, **kw):
    h = dict(AUTH)
    if headers is not None:
        h = headers
    if isinstance(body, (bytes, bytearray)):
        return client.post("/api/blackboard", content=body, headers=h, **kw)
    return client.post("/api/blackboard", json=body, headers=h, **kw)


def _ids(client, since=None):
    params = {} if since is None else {"since": since}
    r = client.get("/api/blackboard", headers=AUTH, params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(body["posts"])
    return body


# ---------------------------------------------------------------- auth

def test_missing_and_wrong_token_are_401(client):
    assert client.get("/api/blackboard").status_code == 401
    assert client.get("/api/blackboard").json() == {"detail": "Bad admin token"}
    bad = {"X-Admin-Token": "nope"}
    assert client.get("/api/blackboard", headers=bad).json() == {"detail": "Bad admin token"}
    assert _post(client, {"text": "secret"}, headers={}).status_code == 401
    assert _post(client, {"text": "secret"}, headers={"X-Admin-Token": ""}).json()["detail"] == "Bad admin token"
    assert _post(client, {"text": "secret"}, headers={"X-Admin-Token": "test-token-x"}).status_code == 401
    empty = _ids(client)  # nothing was stored
    assert empty["posts"] == [] and empty["count"] == 0


def test_previous_token_is_exact_and_untrimmed(monkeypatch):
    """Rotation grace is the raw env value. Spaces are part of the secret."""
    assert server._exact_token("old-token ", "old-token ") is True
    assert server._exact_token("old-token", "old-token ") is False
    assert server._exact_token(" old-token ", "old-token ") is False
    assert server._exact_token("old-token-x", "old-token") is False
    assert server._exact_token("old-toke", "old-token") is False
    assert server._exact_token("", "") is False
    assert server._exact_token(None, "old-token") is False
    assert server._exact_token(" ", " ") is True
    assert server._exact_token("é-token", "é-token") is True
    assert server._exact_token("e-token", "é-token") is False
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN_PREVIOUS", " grace ")
    from avrone.config import Settings
    assert Settings().admin_token_previous == " grace "
    monkeypatch.delenv("AVRONE_ADMIN_TOKEN_PREVIOUS", raising=False)
    assert Settings().admin_token_previous == ""


def test_previous_admin_token_opens_blackboard_only(client, monkeypatch):
    previous = "old-token "
    monkeypatch.setattr(server.settings, "admin_token_previous", previous)
    prev = {"X-Admin-Token": previous}
    trimmed = {"X-Admin-Token": "old-token"}
    padded = {"X-Admin-Token": " old-token "}
    assert client.get("/api/blackboard", headers=prev).status_code == 200
    posted = _post(client, {"id": "grace-1", "text": "during rotation", "ts": 8}, headers=prev)
    assert posted.status_code == 200
    assert posted.json() == {"posted": True, "duplicate": False, "id": "grace-1"}
    assert _ids(client)["posts"][0]["text"] == "during rotation"
    # The current token still works, and neither a trim nor a neighbor does.
    assert client.get("/api/blackboard", headers=AUTH).status_code == 200
    assert client.get("/api/blackboard", headers=trimmed).status_code == 401
    assert client.get("/api/blackboard", headers=padded).status_code == 401
    assert client.get("/api/blackboard", headers={"X-Admin-Token": "test-token "}).status_code == 401
    # Other admin routes ignore the grace token.
    assert client.post("/api/terminal", json={"cmd": "echo hi"}, headers=prev).status_code == 401
    assert client.get("/api/fleet", headers=prev).status_code == 401
    assert client.post("/api/terminal", json={"cmd": "echo hi"}, headers=AUTH).status_code == 200
    # Unset current token still disables the route, even with a previous secret.
    monkeypatch.setattr(server.settings, "admin_token", "")
    assert client.get("/api/blackboard", headers=prev).status_code == 403
    assert _post(client, {"text": "no"}, headers=prev).status_code == 403


def test_boot_id_is_stable_for_the_process_and_changes_on_restart(client):
    import re
    import subprocess
    import sys
    status = client.get("/api/status").json()
    board = client.get("/api/blackboard", headers=AUTH).json()
    again = client.get("/api/status").json()
    board_again = client.get("/api/blackboard?since=missing", headers=AUTH).json()
    boot = status["boot"]
    assert isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{32}", boot)
    assert boot == server.boot_id() == again["boot"] == board["boot"] == board_again["boot"]

    def spawned() -> str:
        out = subprocess.check_output(
            [sys.executable, "-c", "from avrone.server import boot_id; print(boot_id(), end='')"],
            text=True,
            timeout=60,
        )
        return out

    first, second = spawned(), spawned()
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert re.fullmatch(r"[0-9a-f]{32}", second)
    assert len({boot, first, second}) == 3


def test_open_reads_stay_open_but_blackboard_does_not(client):
    """AVRONE_PUBLIC_ACCESS=all lets /api/runs through. The blackboard still wants the token."""
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/blackboard").status_code == 401


def test_admin_off_is_403_and_oversize_still_413(client, monkeypatch):
    monkeypatch.setattr(server.settings, "admin_token", "")
    r = client.get("/api/blackboard", headers=AUTH)
    assert r.status_code == 403
    assert r.json() == {"detail": "Admin access is off. Set AVRONE_ADMIN_TOKEN to enable it."}
    r = _post(client, {"text": "nope"})
    assert r.status_code == 403
    huge = b"{" + b"x" * 5000
    r = client.post("/api/blackboard", content=huge, headers=AUTH)
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large (max 4096 bytes)"}
    assert "AVRONE_MAX_BODY_BYTES" not in r.text


def test_oversize_is_413_before_the_token_is_checked(client, monkeypatch):
    seen = []
    real = server.require_blackboard_admin

    def spy(token):
        seen.append(token)
        return real(token)

    monkeypatch.setattr(server, "require_blackboard_admin", spy)
    r = client.post("/api/blackboard", content=b"x" * 4097, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large (max 4096 bytes)"}
    assert r.headers.get("connection") == "close"
    assert seen == []  # the route, and therefore auth, never ran
    assert _ids(client)["count"] == 0
    seen.clear()
    # Just under the cap, a bad token is an auth failure. Exactly 4096 is still not 413.
    pad = b'{"text":"' + (b"a" * (4096 - len(b'{"text":"' ) - len(b'"}'))) + b'"}'
    assert len(pad) == 4096
    r = client.post("/api/blackboard", content=pad, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401
    assert seen == ["nope"]
    assert _ids(client)["count"] == 0


def test_blackboard_cap_does_not_shrink_other_routes():
    assert server.max_body_bytes("/api/blackboard") == 4096
    assert server.max_body_bytes("/api/chat") == server.max_body_bytes()
    assert server.max_body_bytes() > 4096
    called = []

    async def app(scope, receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = server.BodyLimit(app)

    async def drive(path, length):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "path": path, "headers": [(b"content-length", str(length).encode())]}
        await mw(scope, receive, send)
        return sent

    chat = asyncio.run(drive("/api/chat", 5000))
    board = asyncio.run(drive("/api/blackboard", 5000))
    assert called == ["/api/chat"]
    assert chat[0]["status"] == 200
    assert board[0]["status"] == 413
    assert json.loads(board[1]["body"]) == {"detail": "request body too large (max 4096 bytes)"}
    assert (b"connection", b"close") in board[0]["headers"]


def test_chunked_oversize_is_413_before_auth(client, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "require_blackboard_admin", lambda token: seen.append(token))

    def gen():
        yield b'{"id":"should-not-store","text":"'
        yield b"y" * 5000
        yield b'"}'

    r = client.post("/api/blackboard", content=gen(), headers={"X-Admin-Token": "nope"})
    assert r.status_code == 413
    assert r.json()["detail"] == "request body too large (max 4096 bytes)"
    assert seen == []
    assert _ids(client)["count"] == 0


# ---------------------------------------------------------------- malformed bodies

@pytest.mark.parametrize("raw", [b"", b"{", b"null", b"[]", b'"hi"', b"123", b"true", b"{]", b"\xff"])
def test_malformed_or_non_object_is_400(client, raw):
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 400
    assert r.json() == {"detail": "body must be JSON"}
    assert _ids(client)["count"] == 0


def test_malformed_with_bad_token_is_401_not_400(client):
    """Size, then auth, then JSON. A small bad body with a bad token is 401."""
    r = client.post("/api/blackboard", content=b"{", headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Bad admin token"}


def test_deeply_nested_json_is_400_not_500(client):
    raw = b"[" * 2000 + b"]" * 2000
    assert len(raw) < 4096
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 400
    assert r.json() == {"detail": "body must be JSON"}


def test_invalid_kind_is_400(client):
    for kind in ("mood", "health", "evidence", "gossip", "Finding!", "  "):
        r = _post(client, {"text": "x", "kind": kind, "id": "k-bad"})
        assert r.status_code == 400, kind
        assert r.json()["detail"] == "kind must be one of " + ", ".join(KINDS)
    assert _ids(client)["count"] == 0


def test_empty_text_is_400(client):
    for text in ("", "   ", "\n\t", None):
        r = _post(client, {"text": text, "kind": "finding"})
        assert r.status_code == 400
        assert r.json()["detail"] == "text is required"
    assert _ids(client)["count"] == 0


@pytest.mark.parametrize("pid", ["bad id", "a/b", "x" * 81, "has space", "semi;colon", "café"])
def test_bad_id_is_400(client, pid):
    r = _post(client, {"id": pid, "text": "x"})
    assert r.status_code == 400
    assert r.json()["detail"] == "bad post id"
    assert _ids(client)["count"] == 0


def test_bridge_reject_is_400(client, monkeypatch):
    import avrone.bridge as bridge_mod

    def boom(core, post):
        raise BridgeError("post rejected")

    monkeypatch.setattr(bridge_mod, "blackboard_accept", boom)
    r = _post(client, {"text": "x", "id": "will-reject"})
    assert r.status_code == 400
    assert r.json()["detail"] == "post rejected"
    assert _ids(client)["count"] == 0


def test_accept_rejects_a_record_the_bridge_cannot_store(client):
    with pytest.raises(BridgeError, match="post rejected"):
        blackboard_accept(server.state["core"], {"id": "not-a-kind", "kind": "nope", "text": "x", "run_id": "run"})


# ---------------------------------------------------------------- happy path, schema, dedup

def test_relay_post_round_trip_and_defaults(client):
    body = {
        "id": "ck-mood-0123456789ab",
        "run_id": "fusion-relay",
        "agent": "cyclekernal:mood",
        "role": "cyclekernal-relay",
        "kind": "CONCLUSION",
        "text": "  {\"mood\":\"convergent\",\"mood_dot\":0.72}  ",
        "refs": [],
        "origin_node": "fusion-bridge",
        "ts": 1700000000.0,
    }
    r = _post(client, body)
    assert r.status_code == 200
    assert r.json() == {"posted": True, "duplicate": False, "id": "ck-mood-0123456789ab"}
    got = _ids(client)
    assert got["count"] == 1
    post = got["posts"][0]
    assert set(post) == POST_KEYS
    assert post["kind"] == "conclusion"
    assert post["text"] == '{"mood":"convergent","mood_dot":0.72}'
    assert post["run_id"] == "fusion-relay"
    assert post["agent"] == "cyclekernal:mood"
    assert post["role"] == "cyclekernal-relay"
    assert post["origin_node"] == "fusion-bridge"
    assert post["ts"] == 1700000000.0
    assert post["refs"] == []

    bare = _post(client, {"text": "  hello  "})
    assert bare.status_code == 200
    assigned = bare.json()["id"]
    assert len(assigned) == 12 and assigned.isalnum()
    row = _ids(client)["posts"][1]
    assert row["id"] == assigned
    assert row["text"] == "hello"
    assert row["kind"] == "finding"
    assert row["run_id"] == "run"
    assert row["agent"] == "agent"
    assert row["role"] == ""
    assert row["origin_node"] == "local"
    assert row["refs"] == []
    assert row["ts"] > 0


@pytest.mark.parametrize("kind", KINDS)
def test_every_bridge_kind_is_accepted(client, kind):
    r = _post(client, {"id": f"k-{kind}", "text": kind, "kind": kind, "ts": 10})
    assert r.status_code == 200 and r.json()["duplicate"] is False
    stored = [p for p in _ids(client)["posts"] if p["id"] == f"k-{kind}"][0]
    assert stored["kind"] == kind


def test_omitted_empty_id_is_assigned(client):
    r = _post(client, {"id": "", "text": "generated"})
    assert r.status_code == 200 and r.json()["duplicate"] is False
    assert r.json()["id"] != ""
    assert _ids(client)["posts"][0]["text"] == "generated"


def test_clips_text_and_fields_and_keeps_unknown_refs(client):
    prefix = b'{"id":"clip1","kind":"finding","text":"'
    suffix = b'"}'
    pad = 4096 - len(prefix) - len(suffix)
    assert pad > 4000
    raw = prefix + (b"q" * pad) + suffix
    assert len(raw) == 4096
    r = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert r.status_code == 200 and r.json()["duplicate"] is False
    text = _ids(client)["posts"][0]["text"]
    assert len(text) == 4000 and text.endswith("...") and text[:-3] == "q" * 3997

    refs = [f"r{i:02d}" for i in range(25)] + ["bad ref", "r00", ""]
    long = {
        "id": "clip2",
        "text": "fields",
        "run_id": "R" * 100,
        "agent": "A" * 100,
        "role": "L" * 100,
        "origin_node": "O" * 100,
        "refs": refs,
        "ts": 4,
        "extra": {"ignored": True},
    }
    assert _post(client, long).status_code == 200
    row = [p for p in _ids(client)["posts"] if p["id"] == "clip2"][0]
    assert row["run_id"] == "R" * 80
    assert row["agent"] == "A" * 80
    assert row["role"] == "L" * 60
    assert row["origin_node"] == "O" * 80
    assert row["refs"] == [f"r{i:02d}" for i in range(20)]
    assert "extra" not in row
    # A string ref is one id; unknown ids are kept (this route is not one run's ref set).
    assert _post(client, {"id": "clip3", "text": "one ref", "refs": "solo-ref", "ts": 5}).status_code == 200
    assert [p for p in _ids(client)["posts"] if p["id"] == "clip3"][0]["refs"] == ["solo-ref"]


def test_non_finite_ts_uses_the_server_clock(client):
    before = time.time()
    assert _post(client, {"id": "ts-bool", "text": "clock", "ts": True}).status_code == 200
    assert _post(client, {"id": "ts-zero", "text": "clock", "ts": 0}).status_code == 200
    assert _post(client, {"id": "ts-word", "text": "clock", "ts": "soon"}).status_code == 200
    after = time.time()
    for row in _ids(client)["posts"]:
        assert before <= row["ts"] <= after + 1
    kept = _post(client, {"id": "ts-kept", "text": "clock", "ts": 42})
    assert kept.status_code == 200
    assert [p for p in _ids(client)["posts"] if p["id"] == "ts-kept"][0]["ts"] == 42


def test_duplicate_does_not_overwrite_and_cross_run_id_is_one_post(client):
    first = _post(client, {
        "id": "dup-1", "text": "original", "kind": "finding", "run_id": "alpha",
        "ts": 3, "origin_node": "local", "agent": "scout",
    })
    assert first.json() == {"posted": True, "duplicate": False, "id": "dup-1"}
    again = _post(client, {
        "id": "dup-1", "text": "replacement", "kind": "conclusion", "run_id": "beta",
        "ts": 99, "origin_node": "other", "agent": "other",
    })
    assert again.status_code == 200
    assert again.json() == {"posted": True, "duplicate": True, "id": "dup-1"}
    posts = _ids(client)["posts"]
    assert [p["id"] for p in posts] == ["dup-1"]
    assert posts[0]["text"] == "original"
    assert posts[0]["kind"] == "finding"
    assert posts[0]["run_id"] == "alpha"
    assert posts[0]["ts"] == 3
    assert posts[0]["agent"] == "scout"
    # The per-run read still shows only that run. beta has no bridge post.
    alpha = client.get("/api/runs/alpha/bridge").json()
    assert alpha["count"] == 1 and alpha["posts"][0]["text"] == "original"
    assert client.get("/api/runs/beta/bridge").status_code == 404


def test_in_process_post_is_on_the_board_and_its_id_is_a_duplicate(client):
    core = server.state["core"]
    local = for_core(core, "squad-9").post("W-1", "question", "what broke")
    board = _ids(client)
    assert [p["id"] for p in board["posts"]] == [local["id"]]
    assert board["posts"][0]["text"] == "what broke"
    r = _post(client, {"id": local["id"], "text": "nope", "kind": "answer"})
    assert r.json()["duplicate"] is True
    assert for_core(core, "squad-9").get(local["id"])["text"] == "what broke"


def test_evicted_id_is_a_duplicate_and_is_not_resurrected(client, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "2")
    for pid, ts in (("ev-a", 1), ("ev-b", 2), ("ev-c", 3)):
        r = _post(client, {"id": pid, "text": pid, "ts": ts, "run_id": "ring"})
        assert r.json()["duplicate"] is False
    board = _ids(client)
    assert [p["id"] for p in board["posts"]] == ["ev-b", "ev-c"]
    # An evicted id is unknown to export, so `since` returns the current ring.
    assert [p["id"] for p in _ids(client, since="ev-a")["posts"]] == ["ev-b", "ev-c"]
    again = _post(client, {"id": "ev-a", "text": "resurrect", "kind": "correction", "ts": 100, "run_id": "ring"})
    assert again.json() == {"posted": True, "duplicate": True, "id": "ev-a"}
    assert [p["id"] for p in _ids(client)["posts"]] == ["ev-b", "ev-c"]
    bridge = for_core(server.state["core"], "ring")
    assert "ev-a" in bridge._evicted
    assert bridge.get("ev-a") is None


# ---------------------------------------------------------------- ordering and pagination

def test_pagination_follows_accept_order(client):
    """Out-of-order timestamps stay in accept order. `since` is the post after that id."""
    specs = [("c-late", 30, "run-c"), ("a-early", 10, "run-a"), ("b-mid", 20, "run-b")]
    for pid, ts, run in specs:
        r = _post(client, {"id": pid, "text": pid, "ts": ts, "run_id": run, "kind": "finding"})
        assert r.json()["duplicate"] is False
    assert [p["id"] for p in _ids(client)["posts"]] == ["c-late", "a-early", "b-mid"]
    assert [p["id"] for p in _ids(client, since="c-late")["posts"]] == ["a-early", "b-mid"]
    assert [p["id"] for p in _ids(client, since="a-early")["posts"]] == ["b-mid"]
    tail = _ids(client, since="b-mid")
    assert tail["posts"] == [] and tail["count"] == 0
    assert [p["id"] for p in _ids(client, since="no-such-id")["posts"]] == ["c-late", "a-early", "b-mid"]
    assert [p["id"] for p in _ids(client, since="")["posts"]] == ["c-late", "a-early", "b-mid"]
    # Each run's own bridge is unchanged; the board is the fan-in.
    assert client.get("/api/runs/run-a/bridge").json()["posts"][0]["id"] == "a-early"
    assert client.get("/api/runs/run-c/bridge").json()["count"] == 1

    walked = []
    cursor = None
    for _ in range(5):
        page = _ids(client)["posts"] if cursor is None else _ids(client, since=cursor)["posts"]
        if not page:
            break
        walked.append(page[0]["id"])
        cursor = page[0]["id"]
    assert walked == ["c-late", "a-early", "b-mid"]


def test_cold_fanin_is_oldest_timestamp_first_and_later_posts_append(client):
    data = server.state["core"].settings.data_dir
    Bridge("disk-z", data).merge_remote(
        [{"id": "zzz", "kind": "finding", "text": "late", "ts": 50, "agent": "z"}], "n1")
    Bridge("disk-a", data).merge_remote(
        [{"id": "mmm", "kind": "question", "text": "tied-m", "ts": 5, "agent": "m"},
         {"id": "aaa", "kind": "finding", "text": "tied-a", "ts": 5, "agent": "a"}], "n1")
    assert [p["id"] for p in _ids(client)["posts"]] == ["aaa", "mmm", "zzz"]
    # Accepted after the cursor exists, even with an older timestamp, so `since` still returns it.
    r = _post(client, {"id": "old-ts", "text": "arrived later", "ts": 1, "kind": "answer", "run_id": "fusion-relay"})
    assert r.json()["duplicate"] is False
    assert [p["id"] for p in _ids(client, since="zzz")["posts"]] == ["old-ts"]
    # Dropping the in-memory cursor reloads the same order from disk.
    core = server.state["core"]
    del core._blackboard_order
    assert [p["id"] for p in blackboard_posts(core)] == ["aaa", "mmm", "zzz", "old-ts"]


def test_agent_post_appends_after_the_cursor(client):
    assert _post(client, {"id": "cursor", "text": "first", "ts": 10}).status_code == 200
    local = for_core(server.state["core"], "squad-1").post("W-1", "finding", "agent saw it")
    page = _ids(client, since="cursor")
    assert [p["id"] for p in page["posts"]] == [local["id"]]
    assert page["posts"][0]["text"] == "agent saw it"
    assert page["posts"][0]["run_id"] == "squad-1"


def test_corrupt_order_file_is_rebuilt(client):
    assert _post(client, {"id": "p2", "text": "second", "ts": 2}).status_code == 200
    assert _post(client, {"id": "p1", "text": "first", "ts": 1}).status_code == 200
    core = server.state["core"]
    path = core.settings.data_dir / "bridge" / "_fanin_order.json"
    path.write_text("not json {{{", encoding="utf-8")
    del core._blackboard_order
    ids = [p["id"] for p in _ids(client)["posts"]]
    assert ids == ["p1", "p2"]  # cold rebuild is oldest timestamp first
    assert json.loads(path.read_text(encoding="utf-8"))["ids"] == ["p1", "p2"]


# ---------------------------------------------------------------- concurrency

def test_concurrent_same_id_stores_one_post(client):
    core = server.state["core"]
    post, err = normalize_blackboard_post({"id": "race-1", "text": "only", "kind": "finding", "ts": 7})
    assert err is None and post is not None
    barrier = threading.Barrier(12)
    results = []
    errors = []

    def go():
        try:
            barrier.wait(timeout=5)
            results.append(blackboard_accept(core, dict(post)))
        except Exception as exc:  # noqa: BLE001 - the test reports the thread failure
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    assert sorted(results) == [(False, "race-1")] + [(True, "race-1")] * 11
    body = _ids(client)
    assert body["count"] == 1 and body["posts"][0]["text"] == "only"


def test_concurrent_unique_ids_keep_a_stable_order(client):
    core = server.state["core"]
    n = 20
    barrier = threading.Barrier(n)
    results = []
    errors = []
    dupes = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            posts = blackboard_posts(core)
            ids = [p["id"] for p in posts]
            if len(ids) != len(set(ids)):
                dupes.append(ids)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()

    def go(i):
        try:
            barrier.wait(timeout=5)
            post, err = normalize_blackboard_post(
                {"id": f"u-{i:02d}", "text": f"t{i}", "ts": i + 1, "kind": "answer", "run_id": "parallel"})
            if err:
                errors.append(err)
                return
            results.append(blackboard_accept(core, post))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    stop.set()
    for thread in readers:
        thread.join(timeout=5)
    assert dupes == []
    assert errors == []
    assert len(results) == n
    assert all(dup is False for dup, _ in results)
    full = blackboard_posts(core)
    assert sorted(p["id"] for p in full) == [f"u-{i:02d}" for i in range(n)]
    assert blackboard_posts(core) == full
    assert blackboard_posts(core, since=full[0]["id"]) == full[1:]


def test_concurrent_http_posts(client):
    n = 8
    barrier = threading.Barrier(n)
    codes = []

    def go(i):
        barrier.wait(timeout=5)
        r = _post(client, {"id": f"http-{i:02d}", "text": f"h{i}", "ts": 100 + i, "kind": "correction"})
        codes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(codes) == n
    assert all(code == 200 and body["duplicate"] is False for code, body in codes)
    body = _ids(client)
    ids = [p["id"] for p in body["posts"]]
    assert sorted(ids) == [f"http-{i:02d}" for i in range(n)]
    walked = []
    cursor = None
    for _ in range(n + 1):
        page = body["posts"] if cursor is None else _ids(client, since=cursor)["posts"]
        if not page:
            break
        walked.append(page[0]["id"])
        cursor = page[0]["id"]
    assert walked == ids
