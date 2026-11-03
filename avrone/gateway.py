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
import json
import os
import re
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
            choice = r.json()["choices"][0]
            msg = choice["message"]
            content = msg.get("content") or ""
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            raise _Retry(f"{up.provider} bad response: {r.text[:200]}", r) from e
        if not isinstance(content, str):
            content = str(content)
        if content.strip():
            return content
        # gpt-oss on Ollama: the action sometimes comes back as a parsed native tool call with
        # empty content. AVRONE's actions are JSON text, so rewrite it into that form instead of
        # handing the agent an empty reply.
        synth = tool_calls_as_action(msg.get("tool_calls"), choice.get("finish_reason"))
        if synth:
            return synth
        if choice.get("finish_reason") == "length" and msg.get("tool_calls"):
            raise _Permanent(f"{up.provider} 200: tool call cut off mid-arguments (finish_reason=length); "
                             f"nothing ran. Raise max tokens or the context window for this model")
        if choice.get("finish_reason") == "length":
            # Every token went to reasoning: retrying gives the same result. Say why plainly.
            raise _Permanent(f"{up.provider} 200: empty reply, output truncated (finish_reason=length); "
                             f"the context window or max tokens is too small for this model")
        return content

    async def chat(self, messages: list[dict[str, str]], model: str | None = None,
                   temperature: float = 0.7, session: str | None = None, lead: bool = False) -> str:
        if getattr(self.s, "llm_mode", "live") == "mock":
            from .llm import _mock_reply
            self.last_route = ["mock"]
            return _mock_reply(messages)
        route: list[str] = []
        errors: list[str] = []
        healthy = [u for u in self.upstreams if u.healthy()] or self.upstreams  # all open: try anyway
        nudge = False
        for up in healthy:
            use_model = model or (up.lead_model if lead and up.lead_model else up.model)
            per_up_tries = max(1, self.s.llm_retries) if up is healthy[0] else 1
            for attempt in range(per_up_tries):
                try:
                    msgs = with_plain_text_nudge(messages) if nudge else messages
                    out = await self._request(up, msgs, use_model, temperature, session)
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
                    r = e.response
                    if r is not None and is_tool_parse_error(r.status_code, r.text):
                        nudge = True
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


# gpt-oss on Ollama: when the model writes its answer into the "commentary"/tool channel, Ollama
# tries to parse it as a native tool call and answers 500 ("error parsing tool call ..."). AVRONE
# never sends native tools; its actions are JSON in plain message text. Re-sending the identical
# request often fails the same way, so the retry carries an explicit plain-text instruction.
TOOL_PARSE_ERR = ("parsing tool call", "parse tool call", "tool call parsing", "tool_call",
                  "harmony", "unexpected end of json input")
PLAIN_TEXT_NUDGE = ("IMPORTANT: do NOT use native function/tool calling. Write your whole reply, "
                    "including any JSON action object, as ordinary message text in the final answer.")


def is_tool_parse_error(status: int, text: str) -> bool:
    t = (text or "").lower()
    return status >= 500 and any(k in t for k in TOOL_PARSE_ERR)


def with_plain_text_nudge(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """A copy of messages whose system prompt carries the plain-text instruction (never mutates)."""
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        if PLAIN_TEXT_NUDGE not in str(out[0].get("content", "")):
            out[0]["content"] = f"{out[0].get('content') or ''}\n\n{PLAIN_TEXT_NUDGE}".strip()
    else:
        out.insert(0, {"role": "system", "content": PLAIN_TEXT_NUDGE})
    return out


def _tool_name(raw: Any) -> str:
    name = str(raw or "").strip()
    # gpt-oss namespaces: "functions.shell", "repo_browser.shell", "assistant<|channel|>commentary to=functions.shell"
    name = name.split("to=")[-1].strip()
    name = name.rsplit(".", 1)[-1]
    name = name.split("<|", 1)[0].strip()
    return name


FINAL_TOOL_NAMES = {"final", "final_answer", "finalanswer"}
_FINAL_KEYS = ("final", "answer", "text", "content", "response", "result")
_HARMONY = re.compile(r"<\|[a-z_]{1,24}\|>")


def _parse_args(raw: Any) -> tuple[Any, bool]:
    """Tool-call arguments -> (args, complete). complete=False means the JSON is cut off or
    unreadable. gpt-oss leaks harmony tokens (<|call|>) or trailing junk after a complete
    object; the first complete object is used then."""
    if raw is None:
        return {}, True
    if not isinstance(raw, str):
        return raw, True
    text = _HARMONY.sub("", raw).strip()
    if not text:
        return {}, True
    try:
        return json.loads(text), True
    except ValueError:
        pass
    start = text.find("{")
    if start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text, start)
            if isinstance(obj, dict):
                return obj, True
        except ValueError:
            pass
    return {"_raw": raw}, False


def _final_from(args: Any) -> Any:
    if isinstance(args, dict):
        for k in _FINAL_KEYS:
            if k in args:
                return args[k]
        if len(args) == 1:
            return next(iter(args.values()))
        return args
    return args


def tool_calls_as_action(tool_calls: Any, finish_reason: Any = None) -> str:
    """OpenAI-style message.tool_calls -> AVRONE's JSON action text ("" when unusable).

    Round 19 guards: a call whose arguments were cut off by finish_reason=length is never
    dispatched (a half-written shell command must not run); a nameless call whose arguments
    are themselves an action ({"tool": ...} / {"final": ...}) is used as that action; a call
    named final/final_answer becomes {"final": ...}; exact duplicate calls run once."""
    if not isinstance(tool_calls, list):
        return ""
    truncated = finish_reason == "length"
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        args, complete = _parse_args(fn.get("arguments"))
        if not complete and truncated:
            continue  # cut off mid-arguments: never run it
        name = _tool_name(fn.get("name"))
        action: dict[str, Any]
        if not name:
            if isinstance(args, dict) and isinstance(args.get("tool"), str) and args["tool"].strip():
                action = {"tool": _tool_name(args["tool"]), "args": args.get("args", {})}
            elif isinstance(args, dict) and "final" in args:
                action = {"final": args["final"]}
            else:
                continue
        elif name.lower() in FINAL_TOOL_NAMES:
            action = {"final": _final_from(args)}
        else:
            if not isinstance(args, dict):
                args = {"_raw": args}
            action = {"tool": name, "args": args}
        key = json.dumps(action, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    if not actions:
        return ""
    finals = [a for a in actions if "final" in a]
    if finals and len(actions) > 1:
        actions = [a for a in actions if "final" not in a]  # tools first; the agent finishes later
    return json.dumps(actions[0] if len(actions) == 1 else {"tools": actions}, default=str)


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
