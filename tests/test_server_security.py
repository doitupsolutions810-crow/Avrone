from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from avrone.server import require_api_access


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "testclient", "localhost"])
def test_api_auth_auto_allows_loopback(host, monkeypatch):
    monkeypatch.setenv("AVRONE_API_AUTH", "auto")
    request = SimpleNamespace(client=SimpleNamespace(host=host))
    require_api_access(request, None)


def test_api_auth_auto_requires_token_for_remote_client(monkeypatch):
    monkeypatch.setenv("AVRONE_API_AUTH", "auto")
    monkeypatch.setenv("AVRONE_API_TOKEN", "t" * 40)
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"))
    with pytest.raises(HTTPException) as exc:
        require_api_access(request, None)
    assert exc.value.status_code == 401
    require_api_access(request, "t" * 40)


def test_api_auth_always_requires_token_even_for_loopback(monkeypatch):
    monkeypatch.setenv("AVRONE_API_AUTH", "always")
    monkeypatch.setenv("AVRONE_API_TOKEN", "t" * 40)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(HTTPException) as exc:
        require_api_access(request, None)
    assert exc.value.status_code == 401
    require_api_access(request, "t" * 40)


def test_invalid_api_auth_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("AVRONE_API_AUTH", "typo")
    monkeypatch.setenv("AVRONE_API_TOKEN", "t" * 40)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(HTTPException) as exc:
        require_api_access(request, None)
    assert exc.value.status_code == 401


def test_api_auth_off_is_explicit_bypass(monkeypatch):
    monkeypatch.setenv("AVRONE_API_AUTH", "off")
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"))
    require_api_access(request, None)


def test_api_auth_accepts_http_only_session_cookie(monkeypatch):
    monkeypatch.setenv("AVRONE_API_AUTH", "always")
    monkeypatch.setenv("AVRONE_API_TOKEN", "t" * 40)
    request = SimpleNamespace(
        client=SimpleNamespace(host="192.0.2.10"),
        cookies={"avrone_api_token": "t" * 40},
    )
    require_api_access(request, None)


def test_websocket_origin_comparison_normalizes_default_ports():
    from avrone.server import _same_websocket_origin

    assert _same_websocket_origin("https://example.com", "example.com")
    assert _same_websocket_origin("https://example.com:443", "example.com")
    assert _same_websocket_origin("http://localhost:8710", "localhost:8710")
    assert not _same_websocket_origin("https://attacker.example", "example.com")
    assert not _same_websocket_origin("file://example.com", "example.com")


def test_api_session_sets_http_only_strict_cookie(monkeypatch):
    from fastapi import Response
    from starlette.requests import Request
    from avrone.server import create_api_session

    monkeypatch.setenv("AVRONE_API_AUTH", "always")
    monkeypatch.setenv("AVRONE_API_TOKEN", "t" * 40)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/session",
        "raw_path": b"/api/auth/session",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"example.com"), (b"x-forwarded-proto", b"https")],
        "client": ("192.0.2.10", 12345),
        "server": ("example.com", 80),
    }
    request = Request(scope)
    response = Response()
    result = __import__("asyncio").run(create_api_session(request, response, "t" * 40))
    cookie = response.headers["set-cookie"].lower()
    assert result["ok"] is True
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "max-age=28800" in cookie


def test_api_session_rejects_wrong_token(monkeypatch):
    from fastapi import HTTPException, Response
    from starlette.requests import Request
    from avrone.server import create_api_session

    monkeypatch.setenv("AVRONE_API_AUTH", "always")
    monkeypatch.setenv("AVRONE_API_TOKEN", "t" * 40)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/auth/session",
        "raw_path": b"/api/auth/session", "query_string": b"", "root_path": "",
        "headers": [(b"host", b"example.com")], "client": ("192.0.2.10", 12345),
        "server": ("example.com", 80),
    }
    with pytest.raises(HTTPException) as exc:
        __import__("asyncio").run(create_api_session(Request(scope), Response(), "wrong"))
    assert exc.value.status_code == 401
