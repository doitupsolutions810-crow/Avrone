"""Fixed-origin Grok Bot adapter. General network tools stay independently gated."""
from __future__ import annotations

import json
import hashlib
import asyncio
import math
import os
import re
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 64 * 1024
MAX_PROMPT = 8000
HTTP_TRANSPORT = None  # dependency injection for contract tests


class ProtocolError(ValueError):
    """A safe, locally generated protocol error, without upstream exception text."""


def strict_json(raw: bytes, *, limit=MAX_BYTES) -> dict:
    if len(raw) > limit:
        raise ProtocolError("response exceeds size limit")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        stack, nodes = [(data, 0)], 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if depth > 64 or nodes > 100_000:
                raise ValueError
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, dict):
                for key, child in value.items():
                    key.encode("utf-8")
                    stack.append((child, depth + 1))
            elif isinstance(value, list):
                stack.extend((child, depth + 1) for child in value)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid JSON response") from None
    if not isinstance(data, dict):
        raise ProtocolError("expected a JSON object")
    return data


def origin(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or any(c in value for c in "\\?#"):
            raise ValueError
        u = urlsplit(value.strip())
        _ = u.port
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password
                or u.path not in {"", "/"} or u.query or u.fragment):
            raise ValueError
        return f"{u.scheme}://{u.netloc}"
    except (ValueError, AttributeError):
        raise ProtocolError("expected an HTTP(S) origin without credentials, path, query or fragment") from None


async def request_json(base: str, path: str, *, token: str = "", payload=None, timeout=5.0):
    """Bound both declared and streamed bytes, block redirects, never return upstream error text."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300 or not math.isfinite(timeout):
        raise ProtocolError("invalid bridge timeout")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = {"timeout": httpx.Timeout(timeout, connect=min(5.0, timeout)),
              "follow_redirects": False, "trust_env": False}
    if HTTP_TRANSPORT is not None:
        kwargs["transport"] = HTTP_TRANSPORT
    async with asyncio.timeout(timeout), httpx.AsyncClient(**kwargs) as client:
        async with client.stream("POST" if payload is not None else "GET", origin(base) + path,
                                 headers=headers, **({"json": payload} if payload is not None else {})) as r:
            if not r.is_success:
                raise ProtocolError(f"HTTP {r.status_code}" + ("; redirects blocked" if r.is_redirect else ""))
            if r.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ProtocolError("encoded response is not supported")
            length = r.headers.get("content-length", "").strip()
            if length and not re.fullmatch(r"[0-9]+", length):
                raise ProtocolError("invalid response length")
            if length and (len(length) > 20 or int(length) > MAX_BYTES):
                raise ProtocolError("response exceeds 64 KiB")
            chunks, total = [], 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ProtocolError("response exceeds 64 KiB")
                chunks.append(chunk)
    return strict_json(b"".join(chunks))


async def ask(prompt: str, *, correlation_id: str = "") -> dict:
    base = os.getenv("AVRONE_GROKBOT_URL", "").strip()
    if not base:
        return {"ok": False, "error": "Grok Bot bridge is disabled; set AVRONE_GROKBOT_URL"}
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        return {"ok": False, "error": "prompt must contain 1–8000 characters"}
    if not isinstance(correlation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{0,80}", correlation_id):
        return {"ok": False, "error": "invalid correlation id"}
    try:
        prompt.encode("utf-8")
        data = await request_json(base, "/api/bridge/research", token=os.getenv("GROK_BRIDGE_TOKEN", ""),
                                  payload={"message": prompt.strip(), "correlation_id": correlation_id}, timeout=180.0)
        answer, receipt = data.get("answer"), data.get("receipt")
        if not isinstance(answer, str) or not answer.strip():
            raise ProtocolError("missing textual answer")
        if (not isinstance(receipt, dict) or receipt.get("agent") != "grok-researcher"
                or not isinstance(receipt.get("run_id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", receipt["run_id"])
                or receipt.get("correlation_id") != correlation_id):
            raise ProtocolError("missing or mismatched research receipt")
        if data.get("ok") is not True:
            raise ProtocolError("upstream research did not complete")
        if receipt.get("answer_sha256") != hashlib.sha256(answer.encode()).hexdigest():
            raise ProtocolError("research answer digest mismatch")
        return {"ok": True, "agent": "grok-researcher", "answer": answer,
                "truncated": False, "receipt": receipt}
    except ProtocolError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


async def t_grokbot_ask(ctx, args):
    result = await ask(args.get("prompt"), correlation_id=ctx.run_id)
    ctx.core.codex.emit(ctx.run_id, "grokbot.bridge", ctx.name,
                        ok=result["ok"], receipt=result.get("receipt"), error=result.get("error"))
    return json.dumps(result, ensure_ascii=False)


def register_grokbot_tools(registry):
    from .tools import Tool
    registry.register(Tool("grokbot.ask", "Ask the configured Grok Bot research agent; returns a correlated receipt. "
                          "Its toolset excludes shell, team spawning and AVRONE delegation.",
                          '{"prompt": str}', t_grokbot_ask,
                          schema={"required": {"prompt": "str"}}))
