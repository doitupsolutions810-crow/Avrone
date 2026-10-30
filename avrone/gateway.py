"""Gateway: route model calls across several providers with fallback + circuit breaking.

Primary upstream = the configured provider (AVRONE_PROVIDER). Add more with:

    AVRONE_LLM_FALLBACKS=openrouter:x-ai/grok-4,ollama:llama3.1

Each entry is provider[:model][|lead_model]; the provider's key comes from its
usual env var (see config.PROVIDERS). On a retryable error (429/5xx/network) the
gateway moves to the next healthy upstream. An upstream that fails repeatedly is
"opened" (skipped) for a cooldown, so a throttled provider stops being hammered.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import PROVIDERS, resolve_provider

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Tests may set this to an httpx transport.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


class LLMError(RuntimeError):
    pass


def is_loopback_url(url: str) -> bool:
    """True for http(s)://127.x / localhost / [::1] URLs: those must bypass any env proxy, or a
    sandbox/corporate HTTP_PROXY swallows calls to the local model server (ConnectError / 404)."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Upstream:
    provider: str
    base_url: str
    model: str
    api_key: str = ""
    lead_model: str = ""
    # breaker state
    fails: int = 0
    open_until: float = 0.0
    calls: int = 0
    errors: int = 0

    def healthy(self) -> bool:
        return time.time() >= self.open_until

    def record(self, ok: bool, threshold: int, cooldown: float) -> None:
        self.calls += 1
        if ok:
            self.fails = 0
            self.open_until = 0.0
        else:
            self.errors += 1
            self.fails += 1
            if self.fails >= threshold:
                self.open_until = time.time() + cooldown

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "state": "open" if not self.healthy() else "closed",
                "fails": self.fails, "calls": self.calls, "errors": self.errors,
                "reopen_in": round(max(0.0, self.open_until - time.time()), 1)}


def parse_upstreams(settings: Any, env: dict[str, str] | None = None) -> list[Upstream]:
    env = os.environ if env is None else env
    ups = [Upstream(settings.provider, settings.llm_base_url, settings.llm_model,
                    settings.llm_api_key, getattr(settings, "lead_model", "") or "")]
    for spec in (env.get("AVRONE_LLM_FALLBACKS", "") or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        head, _, lead = spec.partition("|")
        provider, _, model = head.partition(":")
        provider = provider.strip().lower()
        if provider not in PROVIDERS:
            continue
        try:
            name, url, mdl, key = resolve_provider(provider, None, model.strip() or None, None, env)
        except ValueError:
            continue
        ups.append(Upstream(name, url, mdl, key, lead.strip()))
    return ups


class Gateway:
    def __init__(self, settings: Any):
        self.s = settings
        self.upstreams = parse_upstreams(settings)
        self.threshold = int(getattr(settings, "breaker_threshold", 3))
        self.cooldown = float(getattr(settings, "breaker_cooldown", 30))
        self.last_route: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"upstreams": [u.status() for u in self.upstreams], "last_route": self.last_route}

    async def _request(self, up: Upstream, messages: list[dict[str, str]], model: str | None,
                       temperature: float, session: str | None) -> str:
        headers = {"Content-Type": "application/json"}
        if up.api_key:
            headers["Authorization"] = f"Bearer {up.api_key}"
        if session and up.provider in {"openclaw", "custom"}:
            headers["x-openclaw-session-key"] = session
        if up.provider == "openrouter":
            headers["X-Title"] = "AVRONE"
        body = {"model": model or up.model, "messages": messages, "temperature": temperature}
        url = up.base_url.rstrip("/") + "/chat/completions"
        kw: dict[str, Any] = {"timeout": self.s.llm_timeout}
        if HTTP_TRANSPORT is not None:
            kw["transport"] = HTTP_TRANSPORT
        if is_loopback_url(url):
            kw["trust_env"] = False  # a local Ollama/LM Studio is never reached through HTTP(S)_PROXY
        async with httpx.AsyncClient(**kw) as client:
            r = await client.post(url, headers=headers, json=body)
        if r.status_code in RETRY_STATUS:
            raise _Retry(f"{up.provider} {r.status_code}: {r.text[:300]}", r)
        if r.status_code >= 400:
            # Permanent for this upstream (bad request, auth): don't retry it, but fail over.
            raise _Permanent(f"{up.provider} {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise _Retry(f"{up.provider} bad response: {r.text[:200]}", r) from e

    async def chat(self, messages: list[dict[str, str]], model: str | None = None,
                   temperature: float = 0.7, session: str | None = None, lead: bool = False) -> str:
        if getattr(self.s, "llm_mode", "live") == "mock":
            from .llm import _mock_reply
            self.last_route = ["mock"]
            return _mock_reply(messages)
        route: list[str] = []
        errors: list[str] = []
        healthy = [u for u in self.upstreams if u.healthy()] or self.upstreams  # all open: try anyway
        for up in healthy:
            use_model = model or (up.lead_model if lead and up.lead_model else up.model)
            per_up_tries = max(1, self.s.llm_retries) if up is healthy[0] else 1
            for attempt in range(per_up_tries):
                try:
                    out = await self._request(up, messages, use_model, temperature, session)
                    up.record(True, self.threshold, self.cooldown)
                    route.append(f"{up.provider}:ok")
                    self.last_route = route
                    return out
                except _Permanent as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(str(e))
                    break  # no point retrying this upstream; fail over to the next
                except _Retry as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(str(e))
                    if attempt < per_up_tries - 1:
                        await asyncio.sleep(_delay(e.response, self.s.llm_backoff, attempt))
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(f"{up.provider} unreachable: {e}")
                except httpx.HTTPError as e:
                    up.record(False, self.threshold, self.cooldown)
                    errors.append(f"{up.provider} {type(e).__name__}: {e}")
            route.append(f"{up.provider}:fail")
        self.last_route = route
        raise LLMError(f"All upstreams failed ({' -> '.join(route)}): {' | '.join(errors[-4:])}")


class _Retry(RuntimeError):
    def __init__(self, msg: str, response: httpx.Response | None = None):
        super().__init__(msg)
        self.response = response


class _Permanent(RuntimeError):
    pass


def _delay(r: httpx.Response | None, base: float, attempt: int) -> float:
    delay = base * (2 ** attempt)
    if r is not None:
        try:
            delay = max(delay, min(float(r.headers.get("retry-after", "")), 30.0))
        except ValueError:
            pass
    return delay
