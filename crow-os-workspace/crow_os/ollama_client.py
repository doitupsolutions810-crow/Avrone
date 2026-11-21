"""Live Ollama HTTP API. Unreachable hosts are reported, never invented."""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import httpx

DEFAULT_HOST = "http://localhost:11434"


def ollama_host() -> str:
    return (os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")


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
