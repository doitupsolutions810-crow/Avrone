"""Validation boundary for remote node dispatch.

Fleet HMAC authenticates transport. This module separately validates the shape and
resource bounds of every request before it can schedule an agent run.
"""
from __future__ import annotations

import ipaddress
import math
import os
import re
from urllib.parse import urlsplit

NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
AGENT_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
ALLOWED_RUN_MODES = frozenset({"chat", "squad", "deep"})
MAX_PROMPT_CHARS = 16_000
MAX_MESSAGE_CHARS = 8_000
MAX_ROUTE_HOPS = 8
MAX_SQUAD_SIZE = 8
MAX_NODES = 256
MAX_NODE_URL_CHARS = 2_048


def _bounded_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def validate_route(value: object, *, max_hops: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("route must be a list of node IDs")
    if len(value) > min(MAX_ROUTE_HOPS, max_hops):
        raise ValueError("route exceeds the configured hop limit")
    route: list[str] = []
    for item in value:
        if not isinstance(item, str) or not NODE_ID_RE.fullmatch(item):
            raise ValueError("route contains an invalid node ID")
        if item in route:
            raise ValueError("route cannot visit a node twice")
        route.append(item)
    return route


def validate_run_payload(payload: object, *, max_hops: int) -> dict:
    """Normalize a remote-run request or raise ValueError before scheduling work."""
    if not isinstance(payload, dict):
        raise ValueError("dispatch payload must be a JSON object")

    route = validate_route(payload.get("route", []), max_hops=max_hops)
    hops = _bounded_int(payload.get("hops", 1), "hops", 0, max_hops)
    if hops + len(route) > max_hops:
        raise ValueError("route plus completed hops exceeds the configured hop limit")

    mode = payload.get("mode", "squad")
    if not isinstance(mode, str) or mode not in ALLOWED_RUN_MODES:
        raise ValueError("mode must be one of: chat, squad, deep")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")

    n = payload.get("n")
    if n is not None:
        n = _bounded_int(n, "n", 1, MAX_SQUAD_SIZE)

    sender = payload.get("from")
    if sender is not None and (not isinstance(sender, str) or not NODE_ID_RE.fullmatch(sender)):
        raise ValueError("from must be a valid node ID")

    origin_run = payload.get("origin_run", "")
    if not isinstance(origin_run, str) or len(origin_run) > 128 or any(ord(c) < 32 for c in origin_run):
        raise ValueError("origin_run must be a string of at most 128 printable characters")

    return {"route": route, "mode": mode, "prompt": prompt, "n": n, "hops": hops,
            "from": sender, "origin_run": origin_run}


def validate_message_payload(payload: object, *, max_hops: int) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("message payload must be a JSON object")
    route = validate_route(payload.get("route", []), max_hops=max_hops)
    hops = _bounded_int(payload.get("hops", 0), "hops", 0, max_hops)
    if hops + len(route) > max_hops:
        raise ValueError("route plus completed hops exceeds the configured hop limit")

    agent = payload.get("agent")
    if not isinstance(agent, str) or not AGENT_NAME_RE.fullmatch(agent) or not agent.strip():
        raise ValueError("agent must be a non-empty printable name of at most 200 characters")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
    sender = payload.get("from", "remote")
    if not isinstance(sender, str) or not sender.strip() or len(sender) > 200 or any(ord(c) < 32 for c in sender):
        raise ValueError("from must be a printable string of at most 200 characters")
    wait = payload.get("wait", False)
    if not isinstance(wait, bool):
        raise ValueError("wait must be a boolean")
    return {"route": route, "hops": hops, "agent": agent, "message": message,
            "from": sender, "wait": wait}


def validate_node_url(value: object) -> str:
    """Accept HTTPS peer URLs, allowing plain HTTP only for loopback test/local nodes."""
    if not isinstance(value, str) or not value or len(value) > MAX_NODE_URL_CHARS:
        raise ValueError("url must be a non-empty URL of bounded length")
    if value != value.strip() or any(ord(c) <= 32 for c in value):
        raise ValueError("url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
        # Accessing .port raises for malformed port specifications.
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("url is malformed") from None
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain embedded credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("node url must be a base URL without path, query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("url port is outside the valid range")

    loopback = hostname.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "http" and not loopback and os.getenv("AVRONE_FLEET_ALLOW_INSECURE_HTTP", "").lower() not in {"1", "true", "yes"}:
        raise ValueError("non-loopback fleet URLs must use HTTPS (or explicitly enable insecure HTTP)")
    return value.rstrip("/")
