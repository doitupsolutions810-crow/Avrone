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
