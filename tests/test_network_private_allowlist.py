import socket

import pytest

from avrone.network_policy import validate_public_http_url


@pytest.mark.asyncio
async def test_exact_private_ip_may_be_explicitly_allowlisted(monkeypatch):
    monkeypatch.setenv("AVRONE_HTTP_PRIVATE_ALLOWLIST", "10.2.3.4")
    assert await validate_public_http_url("http://10.2.3.4:8710", mock_transport=True) == "http://10.2.3.4:8710"


@pytest.mark.asyncio
async def test_private_hostname_requires_exact_allowlist_and_private_dns_answer(monkeypatch):
    def fake_getaddrinfo(host, port, family, socktype):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.2.3.4", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.delenv("AVRONE_HTTP_PRIVATE_ALLOWLIST", raising=False)
    with pytest.raises(ValueError, match="non-public IP"):
        await validate_public_http_url("https://db.internal")

    monkeypatch.setenv("AVRONE_HTTP_PRIVATE_ALLOWLIST", "db.internal")
    assert await validate_public_http_url("https://db.internal") == "https://db.internal"


@pytest.mark.asyncio
async def test_private_cidr_and_wildcard_are_not_allowlist_entries(monkeypatch):
    def fake_getaddrinfo(host, port, family, socktype):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("AVRONE_HTTP_PRIVATE_ALLOWLIST", "10.0.0.0/8,*.internal")
    with pytest.raises(ValueError, match="non-public IP"):
        await validate_public_http_url("https://db.internal")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8710",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]:8710",
])
async def test_private_allowlist_never_opens_loopback_or_link_local(monkeypatch, url):
    monkeypatch.setenv(
        "AVRONE_HTTP_PRIVATE_ALLOWLIST",
        "127.0.0.1,169.254.169.254,::1,10.0.0.0/8",
    )
    with pytest.raises(ValueError):
        await validate_public_http_url(url, mock_transport=True)
