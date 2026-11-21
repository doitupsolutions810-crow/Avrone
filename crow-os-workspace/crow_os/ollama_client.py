"""Live Ollama HTTP API. Unreachable hosts are reported, never invented."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Iterator

import httpx

DEFAULT_HOST = "http://localhost:11434"


def ollama_host() -> str:
    return (os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")


def ollama_binary() -> dict[str, str]:
    """Locate the host `ollama` binary. Absence is reported, not invented."""
    path = shutil.which("ollama") or ""
    if not path:
        return {"found": "no", "path": "", "version": ""}
    try:
        completed = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"found": "yes", "path": path, "version": f"{type(exc).__name__}"}
    version = (completed.stdout or completed.stderr or "").strip().splitlines()
    return {"found": "yes", "path": path, "version": version[0] if version else ""}


def list_models(host: str | None = None, *, timeout: float = 3.0) -> dict[str, Any]:
    probe = probe_ollama(host, timeout=timeout)
    if probe["state"] != "reachable":
        return probe | {"installed": []}
    base = probe["host"]
    try:
        response = httpx.get(f"{base}/api/tags", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {
            "state": "unreachable",
            "detail": "Ollama unreachable",
            "host": base,
            "error": f"{type(exc).__name__}: {exc}",
            "models": [],
            "installed": [],
        }
    installed = []
    for item in payload.get("models") or []:
        name = item.get("name") or item.get("model") or ""
        if not name:
            continue
        installed.append(
            {
                "name": name,
                "size": item.get("size"),
                "digest": item.get("digest") or "",
                "modified_at": item.get("modified_at") or "",
            }
        )
    probe["installed"] = installed
    probe["models"] = [item["name"] for item in installed]
    probe["detail"] = f"{len(installed)} model(s) at {base}"
    return probe


def probe_ollama(host: str | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
    base = (host or ollama_host()).rstrip("/")
    try:
        response = httpx.get(f"{base}/api/tags", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # network, timeout, non-JSON, HTTP error
        return {
            "state": "unreachable",
            "detail": "Ollama unreachable",
            "host": base,
            "error": f"{type(exc).__name__}: {exc}",
            "models": [],
        }
    models = []
    for item in payload.get("models") or []:
        name = item.get("name") or item.get("model")
        if name:
            models.append(name)
    return {"state": "reachable", "detail": f"{len(models)} model(s) at {base}", "host": base, "error": "", "models": models}


def generate(host: str, model: str, prompt: str, *, timeout: float = 120.0) -> Iterator[str]:
    """Stream response fragments from POST /api/generate. Yields text pieces only."""
    with httpx.stream(
        "POST",
        f"{host.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": True},
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            piece = payload.get("response") or ""
            if piece:
                yield piece
            if payload.get("done"):
                break


def iter_chat(
    host: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream POST /api/chat. Yields {type: token|tool|done|error}."""
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    timeout = httpx.Timeout(10.0, read=None, write=60.0, pool=10.0)
    try:
        with httpx.stream("POST", f"{host.rstrip('/')}/api/chat", json=body, timeout=timeout) as response:
            response.raise_for_status()
            tool_bucket: dict[int, dict[str, Any]] = {}
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = payload.get("message") or {}
                piece = message.get("content") or ""
                if piece:
                    yield {"type": "token", "text": piece}
                for index, call in enumerate(message.get("tool_calls") or []):
                    _merge_tool_call(tool_bucket, index, call)
                if payload.get("done"):
                    for index in sorted(tool_bucket):
                        yield {"type": "tool", "call": tool_bucket[index]}
                    yield {"type": "done"}
                    return
    except Exception as exc:
        yield {"type": "error", "error": f"{type(exc).__name__}: {exc}"}


def iter_pull(host: str, name: str) -> Iterator[dict[str, Any]]:
    """Stream POST /api/pull. Yields Ollama progress objects."""
    timeout = httpx.Timeout(10.0, read=None, write=60.0, pool=10.0)
    with httpx.stream(
        "POST",
        f"{host.rstrip('/')}/api/pull",
        json={"name": name, "stream": True},
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield {
                "status": payload.get("status") or "",
                "digest": payload.get("digest") or "",
                "total": payload.get("total"),
                "completed": payload.get("completed"),
                "error": payload.get("error") or "",
            }


def _merge_tool_call(bucket: dict[int, dict[str, Any]], index: int, call: dict[str, Any]) -> None:
    function = call.get("function") or {}
    current = bucket.setdefault(index, {"name": "", "arguments": {}})
    if function.get("name"):
        current["name"] = function["name"]
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        current["arguments"] = arguments
    elif isinstance(arguments, str) and arguments:
        current["arguments_json"] = current.get("arguments_json", "") + arguments
        try:
            current["arguments"] = json.loads(current["arguments_json"])
        except json.JSONDecodeError:
            current["arguments"] = {}
