import socket

import httpx
import pytest

from avrone.network_policy import MAX_RESPONSE_BYTES, safe_http_request, validate_public_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8710/api/status",
        "http://10.0.0.5/internal",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/admin",
        "http://localhost/",
        "http://secret.local/",
        "https://user:password@example.com/",
        "file:///etc/passwd",
    ],
)
@pytest.mark.asyncio
async def test_blocks_local_private_metadata_and_non_http_targets(url):
    with pytest.raises(ValueError):
        await validate_public_http_url(url, mock_transport=True)


@pytest.mark.asyncio
async def test_blocks_dns_answers_that_resolve_to_private_addresses(monkeypatch):
    def fake_getaddrinfo(host, port, family, socktype):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.2.3.4", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public IP"):
        await validate_public_http_url("https://service.example")


@pytest.mark.asyncio
async def test_allows_public_dns_answer(monkeypatch):
    def fake_getaddrinfo(host, port, family, socktype):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert await validate_public_http_url("https://public.example") == "https://public.example"


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_blocked_before_second_request():
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"}, request=request)

    transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="non-public IP"):
        await safe_http_request("GET", "https://public.test/start", transport=transport)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cross_origin_redirect_drops_authentication_headers():
    observed = []

    async def handler(request):
        observed.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "first.test":
            return httpx.Response(302, headers={"location": "https://second.test/target"}, request=request)
        return httpx.Response(200, text="ok", request=request)

    response = await safe_http_request(
        "GET",
        "https://first.test/start",
        headers={"Authorization": "Bearer secret"},
        transport=httpx.MockTransport(handler),
    )
    assert response.status_code == 200
    assert observed[0][1] == "Bearer secret"
    assert observed[1][1] is None


@pytest.mark.asyncio
async def test_write_requests_do_not_follow_redirects():
    async def handler(request):
        return httpx.Response(302, headers={"location": "https://other.test/target"}, request=request)

    with pytest.raises(ValueError, match="redirects are disabled"):
        await safe_http_request(
            "POST", "https://first.test/start", transport=httpx.MockTransport(handler), json={"x": 1}
        )


@pytest.mark.asyncio
async def test_http_response_body_is_bounded():
    async def handler(request):
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1), request=request)

    with pytest.raises(ValueError, match="decoded bytes"):
        await safe_http_request("GET", "https://public.test/large", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_http_request_header_and_body_limits_apply_before_network():
    async def handler(request):
        raise AssertionError("request should be rejected before reaching transport")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="request body exceeds"):
        await safe_http_request(
            "POST", "https://target.test/", transport=transport, content=b"x" * 1_000_001
        )
    with pytest.raises(ValueError, match="too many headers"):
        await safe_http_request(
            "GET", "https://target.test/", transport=transport,
            headers={f"X-{n}": "x" for n in range(65)},
        )
    with pytest.raises(ValueError, match="control characters"):
        await safe_http_request(
            "GET", "https://target.test/", transport=transport,
            headers={"X-Bad": "ok\r\nInjected: yes"},
        )
