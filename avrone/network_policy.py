"""Bounded outbound HTTP policy for agent-directed URL requests.

URL validation blocks loopback/private/link-local/reserved destinations and validates
DNS answers before connecting. A network egress firewall remains the hard boundary
against DNS rebinding and should be used for hosted deployments.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_URL_CHARS = 2_048
MAX_REDIRECTS = 4
_BLOCKED_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data.ec2.internal",
    "metadata.tencentyun.com",
}


def _parse_url(url: object) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ValueError("URL must be a non-empty string of at most 2048 characters")
    if url != url.strip() or any(ord(char) <= 32 for char in url):
        raise ValueError("URL must not contain whitespace or control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("URL is malformed") from None
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("URL must be absolute http(s)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("URL fragments are not accepted")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is outside the valid range")
    host = host.rstrip(".").lower()
    if not host:
        raise ValueError("URL hostname is missing")
    if (host in _BLOCKED_HOSTS or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))
            or host in {"0.0.0.0", "::"}):
        raise ValueError("URL targets a local or metadata hostname")
    return host, parsed.scheme, port or (443 if parsed.scheme == "https" else 80)


async def validate_public_http_url(url: object, *, mock_transport: bool = False) -> str:
    host, scheme, port = _parse_url(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("URL resolves to a non-public IP address")
    elif not mock_transport:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
            )
        except (OSError, socket.gaierror):
            raise ValueError("URL hostname could not be resolved safely") from None
        addresses = {record[4][0].split("%", 1)[0] for record in records}
        if not addresses:
            raise ValueError("URL hostname has no addresses")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise ValueError("DNS returned an invalid IP address") from None
            if not ip.is_global:
                raise ValueError("URL hostname resolves to a non-public IP address")
    return str(url)


async def safe_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    transport: httpx.AsyncBaseTransport | None = None,
    max_redirects: int = MAX_REDIRECTS,
    **kwargs,
) -> httpx.Response:
    """Request a validated public URL and revalidate every redirect before following."""
    current = await validate_public_http_url(url, mock_transport=transport is not None)
    method = method.upper()
    current_headers = dict(headers or {})
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                 transport=transport) as client:
        for redirect_count in range(max_redirects + 1):
            response = await client.request(method, current, headers=current_headers, **kwargs)
            if response.status_code not in {301, 302, 303, 307, 308} or "location" not in response.headers:
                return response
            if method not in {"GET", "HEAD"}:
                raise ValueError("redirects are disabled for write requests")
            if redirect_count >= max_redirects:
                raise ValueError(f"too many HTTP redirects (maximum {max_redirects})")
            target = urljoin(current, response.headers["location"])
            await validate_public_http_url(target, mock_transport=transport is not None)
            old, new = urlsplit(current), urlsplit(target)
            if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                current_headers = {
                    key: value for key, value in current_headers.items()
                    if key.lower() not in {"authorization", "cookie", "proxy-authorization", "host"}
                }
            current = target
    raise ValueError("HTTP redirect processing did not complete")
