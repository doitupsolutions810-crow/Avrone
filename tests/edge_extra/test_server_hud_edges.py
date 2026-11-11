"""Round 4 HUD / FastAPI edges: blackboard methods, content-type, query strings, telemetry."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from avrone import server

AUTH = {"X-Admin-Token": "test-token"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def test_blackboard_wrong_content_type_still_parses_json(client):
    """The handler reads the raw body; a valid JSON object must work even if Content-Type is wrong."""
    body = json.dumps({"kind": "finding", "text": "plain-typed", "id": "plaintyped01"}).encode()
    r = client.post("/api/blackboard", content=body,
                    headers={**AUTH, "Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    assert r.json()["posted"] is True


def test_blackboard_empty_body_and_octet_stream_are_400(client):
    r = client.post("/api/blackboard", content=b"",
                    headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/blackboard", content=b"\xff\xfe not utf-8",
                    headers={**AUTH, "Content-Type": "application/octet-stream"})
    assert r.status_code == 400


def test_blackboard_emoji_and_unknown_fields_round_trip(client):
    body = {
        "kind": "finding",
        "text": "hello 🌍 café",
        "id": "emojiround01",
        "run_id": "run-🌍",
        "agent": "Scout",
        "extra": {"nested": True, "n": 1},
        "unexpected_flag": True,
    }
    r = client.post("/api/blackboard", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    got = client.get("/api/blackboard", headers=AUTH)
    assert got.status_code == 200
    posts = got.json()["posts"]
    assert any(p["id"] == "emojiround01" and "🌍" in p["text"] for p in posts)
    # Response must be strict UTF-8 JSON.
    json.dumps(got.json(), ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_blackboard_surrogate_key_does_not_500(client):
    raw = b'{"kind": "finding", "text": "ok", "id": "surrkey00001", "\xed\xa0\x80": "nope"}'
    # A lone-surrogate key may fail utf-8 decode (400) or be accepted; never 5xx.
    r = client.post("/api/blackboard", content=raw,
                    headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code < 500


def test_blackboard_head_options_put_delete_not_5xx(client):
    for method in ("head", "options", "put", "patch", "delete"):
        r = getattr(client, method)("/api/blackboard", headers=AUTH)
        assert r.status_code < 500, (method, r.status_code, r.text)
        assert r.status_code in {200, 204, 405, 400, 401, 403, 404, 415, 422}


def test_blackboard_huge_since_query_does_not_500(client):
    huge = "a" * 20_000
    r = client.get("/api/blackboard", headers=AUTH, params={"since": huge})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == len(r.json()["posts"])


def test_blackboard_since_traversal_and_emoji(client):
    r = client.post("/api/blackboard", json={"kind": "finding", "text": "x", "id": "sinceok00001"},
                    headers=AUTH)
    assert r.status_code == 200
    for since in ("../etc/passwd", "since/../../x", "🌍" * 50, "pre\x00post"):
        g = client.get("/api/blackboard", headers=AUTH, params={"since": since})
        assert g.status_code == 200, (since, g.text)


def test_status_and_tools_accept_head(client):
    for path in ("/api/status", "/api/tools", "/"):
        r = client.head(path)
        assert r.status_code < 500, (path, r.status_code)


def test_telemetry_junk_hours_do_not_500(client):
    # FastAPI may 422 a non-float, or accept nan/inf; the handler must not 5xx.
    for hours in ("nan", "inf", "-inf", "-1", "1e400", "abc", "24"):
        r = client.get("/api/telemetry", headers=AUTH, params={"hours": hours})
        assert r.status_code < 500, (hours, r.status_code, r.text)
        if r.status_code == 200:
            json.dumps(r.json(), allow_nan=False)


def test_map_junk_format_and_hours_do_not_500(client):
    for fmt, hours in (("json", "nan"), ("mermaid", "24"), ("../../etc", "1"), ("", "abc")):
        r = client.get("/api/map", headers=AUTH, params={"format": fmt, "hours": hours})
        assert r.status_code < 500, (fmt, hours, r.status_code, r.text)


def test_checkpoints_path_traversal_is_not_5xx(client):
    for ck in ("../etc/passwd", "..%2F..%2Fsecrets", "....", "a" * 500):
        r = client.get(f"/api/checkpoints/{ck}", headers=AUTH)
        assert r.status_code < 500, (ck, r.status_code)


def test_concurrent_blackboard_get_and_post(client):
    import threading
    errors = []

    def post(i):
        try:
            r = client.post("/api/blackboard",
                            json={"kind": "finding", "text": f"c{i}", "id": f"concget{i:04d}"},
                            headers=AUTH)
            if r.status_code != 200:
                errors.append(("post", i, r.status_code, r.text))
        except Exception as e:
            errors.append(("post", i, type(e).__name__, str(e)))

    def get():
        try:
            r = client.get("/api/blackboard", headers=AUTH)
            if r.status_code != 200:
                errors.append(("get", r.status_code, r.text))
            json.dumps(r.json(), allow_nan=False)
        except Exception as e:
            errors.append(("get", type(e).__name__, str(e)))

    threads = [threading.Thread(target=post, args=(i,)) for i in range(8)]
    threads += [threading.Thread(target=get) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_main_rejects_junk_and_out_of_range_ports(monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(host=host, port=port))
    monkeypatch.delenv("AVRONE_HOST", raising=False)
    for raw in ("not-a-port", "", "0", "65536", "-1", "8710abc"):
        seen.clear()
        monkeypatch.setenv("AVRONE_PORT", raw)
        server.main()
        assert seen["port"] == 8710, raw
        assert seen["host"] == "127.0.0.1"
    monkeypatch.setenv("AVRONE_PORT", "9999")
    seen.clear()
    server.main()
    assert seen["port"] == 9999


def test_telemetry_and_system_map_reject_non_finite_windows(client):
    from types import SimpleNamespace
    from avrone.telemetry import _finite_window, system_map, telemetry

    c = server.core()
    for junk in (float("nan"), float("inf"), float("-inf"), -5, "24", None, True):
        out = telemetry(c, junk)
        assert out["window_s"] == 86400 or (isinstance(out["window_s"], (int, float)) and out["window_s"] >= 0)
        json.dumps(out, default=str, allow_nan=False)
        g = system_map(c, junk)
        assert isinstance(g, dict)
    assert _finite_window(10) == 10
    assert _finite_window(float("nan")) == 86400
