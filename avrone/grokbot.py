"""Fixed-origin Grok Bot adapter. General network tools stay independently gated."""
from __future__ import annotations

import json
import hashlib
import os
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 64 * 1024
MAX_PROMPT = 8000
HTTP_TRANSPORT = None  # dependency injection for contract tests


def origin(value: str) -> str:
    try:
        u = urlsplit(value.strip())
        _ = u.port
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password
                or u.path not in {"", "/"} or u.query or u.fragment):
            raise ValueError
        return f"{u.scheme}://{u.netloc}"
    except (ValueError, AttributeError):
        raise ValueError("expected an HTTP(S) origin without credentials, path, query or fragment") from None


async def request_json(base: str, path: str, *, token: str = "", payload=None, timeout=5.0):
    """Bound both declared and streamed bytes, block redirects, never return upstream error text."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = {"timeout": httpx.Timeout(timeout, connect=min(5.0, timeout)),
              "follow_redirects": False, "trust_env": False}
    if HTTP_TRANSPORT is not None:
        kwargs["transport"] = HTTP_TRANSPORT
    async with httpx.AsyncClient(**kwargs) as client:
        async with client.stream("POST" if payload is not None else "GET", origin(base) + path,
                                 headers=headers, **({"json": payload} if payload is not None else {})) as r:
            if not r.is_success:
                raise ValueError(f"HTTP {r.status_code}" + ("; redirects blocked" if r.is_redirect else ""))
            length = r.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise ValueError("response exceeds 64 KiB")
            chunks, total = [], 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("response exceeds 64 KiB")
                chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid JSON response") from None
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


async def ask(prompt: str, *, correlation_id: str = "") -> dict:
    base = os.getenv("AVRONE_GROKBOT_URL", "").strip()
    if not base:
        return {"ok": False, "error": "Grok Bot bridge is disabled; set AVRONE_GROKBOT_URL"}
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        return {"ok": False, "error": "prompt must contain 1–8000 characters"}
    try:
        data = await request_json(base, "/api/bridge/research", token=os.getenv("GROK_BRIDGE_TOKEN", ""),
                                  payload={"message": prompt.strip(), "correlation_id": correlation_id}, timeout=180.0)
        answer, receipt = data.get("answer"), data.get("receipt")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("missing textual answer")
        if (not isinstance(receipt, dict) or receipt.get("agent") != "grok-researcher"
                or not isinstance(receipt.get("run_id"), str) or not receipt["run_id"]
                or receipt.get("correlation_id") != correlation_id):
            raise ValueError("missing or mismatched research receipt")
        if data.get("ok") is not True:
            raise ValueError("upstream research did not complete")
        if receipt.get("answer_sha256") != hashlib.sha256(answer.encode()).hexdigest():
            raise ValueError("research answer digest mismatch")
        return {"ok": True, "agent": "grok-researcher", "answer": answer[:12000],
                "truncated": len(answer) > 12000, "receipt": receipt}
    except ValueError as e:
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
