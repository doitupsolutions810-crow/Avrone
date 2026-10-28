"""Bounded outbound HTTP policy for agent-directed URL requests.

URL validation blocks loopback/private/link-local/reserved destinations and validates
DNS answers before connecting. A network egress firewall remains the hard boundary
against DNS rebinding and should be used for hosted deployments.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx

MAX_URL_CHARS = 2_048
MAX_REDIRECTS = 4
MAX_RESPONSE_BYTES = 4_000_000
MAX_REQUEST_BYTES = 1_000_000
MAX_HEADERS = 64
MAX_HEADER_BYTES = 32_768
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
            records = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM),
                timeout=3.0,
            )
        except (OSError, socket.gaierror, asyncio.TimeoutError):
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
    """Request a validated public URL, bounding response bytes and revalidating redirects."""
    current = await validate_public_http_url(url, mock_transport=transport is not None)
    method = method.upper()
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300:
        raise ValueError("HTTP timeout must be between 0 and 300 seconds")
    current_headers = dict(headers or {})
    if len(current_headers) > MAX_HEADERS:
        raise ValueError(f"HTTP request has too many headers (maximum {MAX_HEADERS})")
    header_bytes = 0
    for key, value in current_headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("HTTP header names and values must be strings")
        if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
            raise ValueError("HTTP headers must not contain control characters")
        header_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8"))
    if header_bytes > MAX_HEADER_BYTES:
        raise ValueError(f"HTTP headers exceed {MAX_HEADER_BYTES} bytes")

    current_kwargs = dict(kwargs)
    if "json" in current_kwargs:
        try:
            request_body_size = len(json.dumps(current_kwargs["json"], ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("HTTP JSON body is not serializable") from None
    elif "content" in current_kwargs:
        request_body = current_kwargs["content"]
        if isinstance(request_body, str):
            request_body_size = len(request_body.encode("utf-8"))
        elif isinstance(request_body, (bytes, bytearray, memoryview)):
            request_body_size = len(request_body)
        else:
            raise ValueError("HTTP request body must be a string or byte sequence")
    elif "data" in current_kwargs:
        request_body = current_kwargs["data"]
        if isinstance(request_body, (str, bytes, bytearray, memoryview)):
            request_body_size = len(request_body.encode("utf-8") if isinstance(request_body, str) else request_body)
        else:
            request_body_size = len(json.dumps(request_body, ensure_ascii=False).encode("utf-8"))
    else:
        request_body_size = 0
    if request_body_size > MAX_REQUEST_BYTES:
        raise ValueError(f"HTTP request body exceeds {MAX_REQUEST_BYTES} bytes")

    deadline = time.monotonic() + float(timeout)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                 transport=transport) as client:
        for redirect_count in range(max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("HTTP request total deadline exhausted")
            async with client.stream(method, current, headers=current_headers,
                                     timeout=remaining, **current_kwargs) as response:
                if response.status_code in {301, 302, 303, 307, 308} and "location" in response.headers:
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
                        for key in ("json", "data", "content"):
                            current_kwargs.pop(key, None)
                    current = target
                    continue

                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError(f"HTTP response exceeds {MAX_RESPONSE_BYTES} decoded bytes")
                    chunks.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(chunks),
                    request=response.request,
                )
    raise ValueError("HTTP redirect processing did not complete")
