"""Optional Cursor cloud-agent and xAI Grok backends.

Missing keys never trigger a network call and never invent a reply.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

CURSOR_API = "https://api.cursor.com"
XAI_API = "https://api.x.ai/v1"
ACTIVE_RUN = {"CREATING", "RUNNING", "WAITING_FOR_BACKGROUND_WORK"}


class ConnectorUnavailable(RuntimeError):
    pass


def cursor_token() -> str:
    return os.environ.get("CURSOR_API_KEY") or ""


def xai_token() -> str:
    return os.environ.get("XAI_API_KEY") or ""


def cursor_status(*, client: httpx.Client | None = None) -> dict[str, Any]:
    token = cursor_token()
    if not token:
        return {
            "state": "missing_key",
            "detail": "CURSOR_API_KEY is not set. Cursor cloud agents are unavailable.",
        }
    try:
        payload = _cursor(client, "GET", "/v1/me", token)
    except httpx.HTTPError as exc:
        return {"state": "error", "detail": f"Cursor API unreachable ({type(exc).__name__})."}
    except ConnectorUnavailable as exc:
        return {"state": "error", "detail": str(exc)}
    return {"state": "ok", "detail": "Cursor cloud agents API responded.", "account": payload.get("apiKeyName") or payload.get("userEmail") or ""}


def launch_cursor_agent(
    *,
    prompt: str,
    repository: str,
    ref: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    token = cursor_token()
    if not token:
        raise ConnectorUnavailable("CURSOR_API_KEY is not set. Cursor cloud agents are unavailable.")
    if not repository:
        raise ConnectorUnavailable("This checkout has no GitHub remote, so a cloud agent was not launched.")
    body = {
        "prompt": {"text": prompt},
        "repos": [{"url": repository, "startingRef": ref or "main"}],
        "autoCreatePR": False,
        "workOnCurrentBranch": False,
    }
    return _cursor(client, "POST", "/v1/agents", token, body)


def cursor_run(agent_id: str, run_id: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    token = cursor_token()
    if not token:
        raise ConnectorUnavailable("CURSOR_API_KEY is not set. Cursor cloud agents are unavailable.")
    return _cursor(client, "GET", f"/v1/agents/{agent_id}/runs/{run_id}", token)


def _cursor(
    client: httpx.Client | None,
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.request(
            method,
            f"{CURSOR_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json=body,
        )
        if response.status_code in {401, 403}:
            raise ConnectorUnavailable(f"Cursor API rejected the key (HTTP {response.status_code}).")
        response.raise_for_status()
        return response.json()
    finally:
        if own:
            http.close()


def xai_status(*, client: httpx.Client | None = None) -> dict[str, Any]:
    token = xai_token()
    if not token:
        return {"state": "missing_key", "detail": "XAI_API_KEY is not set. Grok is unavailable."}
    own = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        response = http.get(f"{XAI_API}/models", headers={"Authorization": f"Bearer {token}"})
        if response.status_code in {401, 403}:
            return {"state": "error", "detail": f"xAI API rejected the key (HTTP {response.status_code})."}
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        return {"state": "error", "detail": f"xAI API unreachable ({type(exc).__name__})."}
    finally:
        if own:
            http.close()
    names = []
    for item in payload.get("data") or payload.get("models") or []:
        name = item.get("id") or item.get("name")
        if name:
            names.append(name)
    return {"state": "ok", "detail": f"{len(names)} model id(s) from api.x.ai.", "models": names}


def iter_xai_chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    client: httpx.Client | None = None,
) -> Iterator[dict[str, Any]]:
    token = xai_token()
    if not token:
        yield {"type": "error", "error": "XAI_API_KEY is not set. Grok is unavailable."}
        return
    body: dict[str, Any] = {"model": model or os.environ.get("XAI_MODEL") or "grok-4.7", "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    own = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(15.0, read=None))
    try:
        with http.stream(
            "POST",
            f"{XAI_API}/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        ) as response:
            if response.status_code in {401, 403}:
                yield {"type": "error", "error": f"xAI API rejected the key (HTTP {response.status_code})."}
                return
            response.raise_for_status()
            tool_bucket: dict[int, dict[str, Any]] = {}
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = ((payload.get("choices") or [{}])[0]).get("delta") or {}
                piece = choice.get("content") or ""
                if piece:
                    yield {"type": "token", "text": piece}
                for call in choice.get("tool_calls") or []:
                    index = int(call.get("index") or 0)
                    current = tool_bucket.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        current["id"] = call["id"]
                    function = call.get("function") or {}
                    if function.get("name"):
                        current["name"] = function["name"]
                    if function.get("arguments"):
                        current["arguments"] += function["arguments"]
            for index in sorted(tool_bucket):
                item = tool_bucket[index]
                try:
                    arguments = json.loads(item["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                yield {"type": "tool", "call": {"name": item["name"], "arguments": arguments, "id": item["id"]}}
            yield {"type": "done"}
    except httpx.HTTPError as exc:
        yield {"type": "error", "error": f"xAI API unreachable ({type(exc).__name__})."}
    finally:
        if own:
            http.close()


def github_https_url(remote: str) -> str:
    """Public https URL for a GitHub remote, without credentials."""
    if not remote:
        return ""
    cleaned = remote.strip()
    if cleaned.startswith("git@"):
        cleaned = cleaned.replace(":", "/", 1).replace("git@", "https://", 1)
    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    if "github.com" not in (parsed.hostname or ""):
        return ""
    path = parsed.path.removesuffix(".git")
    return f"https://github.com{path}"
