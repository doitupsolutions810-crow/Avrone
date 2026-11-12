"""Read-only Ollama model capability and runtime discovery for AVRONE."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


class OllamaCapabilityError(RuntimeError):
    """The configured Ollama server could not provide model metadata."""


def _native_base(base_url: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise OllamaCapabilityError("configured Ollama base URL is invalid")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _tag_key(value: Any) -> str:
    name = str(value or "").strip()
    return name[:-7] if name.endswith(":latest") else name


def _context_lengths(value: Any) -> list[int]:
    found: list[int] = []
    if not isinstance(value, dict):
        return found
    for key, item in value.items():
        if "context_length" in str(key).lower():
            try:
                number = int(item)
                if number > 0:
                    found.append(number)
            except (TypeError, ValueError):
                pass
        if isinstance(item, dict):
            found.extend(_context_lengths(item))
    return found


def _num_ctx(parameters: Any) -> int | None:
    if not isinstance(parameters, str):
        return None
    match = re.search(r"(?im)^\s*num_ctx\s+(\d+)\s*$", parameters)
    return int(match.group(1)) if match else None


async def describe_ollama_model(
    openai_base_url: str,
    model: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Read model metadata, installed tag and loaded runtime from Ollama native endpoints.

    Capability flags come only from the explicit show-model capabilities field.
    Missing metadata is None, not an inferred false. Tags and running-model inventory
    are best-effort so partially compatible servers still return usable metadata.
    """
    base = _native_base(openai_base_url)
    if not model or len(model) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model):
        raise OllamaCapabilityError("model tag is invalid")

    issues: list[str] = []
    timeout = httpx.Timeout(12.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False) as client:
        try:
            response = await client.post(f"{base}/api/show", json={"model": model})
            response.raise_for_status()
            show = response.json()
            if not isinstance(show, dict):
                raise ValueError("invalid show response")
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaCapabilityError(
                f"Ollama model metadata unavailable for {model} ({type(exc).__name__})"
            ) from None

        tags_known = False
        tags: dict[str, Any] | None = None
        try:
            response = await client.get(f"{base}/api/tags")
            response.raise_for_status()
            tags_payload = response.json()
            tags_list = tags_payload.get("models", []) if isinstance(tags_payload, dict) else []
            if not isinstance(tags_list, list):
                raise ValueError("invalid model inventory")
            tags_known = True
            tags = next((entry for entry in tags_list
                         if isinstance(entry, dict) and _tag_key(entry.get("name") or entry.get("model")) == _tag_key(model)), None)
        except (httpx.HTTPError, ValueError, TypeError):
            issues.append("installed model inventory unavailable")

        runtime_known = False
        loaded: dict[str, Any] | None = None
        try:
            response = await client.get(f"{base}/api/ps")
            response.raise_for_status()
            ps_payload = response.json()
            ps_list = ps_payload.get("models", []) if isinstance(ps_payload, dict) else []
            if not isinstance(ps_list, list):
                raise ValueError("invalid running model inventory")
            runtime_known = True
            loaded = next((entry for entry in ps_list
                           if isinstance(entry, dict) and _tag_key(entry.get("name") or entry.get("model")) == _tag_key(model)), None)
        except (httpx.HTTPError, ValueError, TypeError):
            issues.append("loaded model runtime inventory unavailable")

        ollama_version: str | None = None
        try:
            response = await client.get(f"{base}/api/version")
            response.raise_for_status()
            version_payload = response.json()
            if isinstance(version_payload, dict) and version_payload.get("version"):
                ollama_version = str(version_payload["version"])
        except (httpx.HTTPError, ValueError, TypeError):
            issues.append("Ollama version unavailable")

    capabilities_raw = show.get("capabilities")
    capabilities = {str(item).lower() for item in capabilities_raw} if isinstance(capabilities_raw, list) else None
    thinking_meta = show.get("thinking")
    thinking_values = thinking_meta.get("values") if isinstance(thinking_meta, dict) else None
    if capabilities is not None:
        thinking_supported = "thinking" in capabilities
    elif isinstance(thinking_values, list):
        thinking_supported = any(value is True or isinstance(value, str) for value in thinking_values)
    else:
        thinking_supported = None

    details = show.get("details") if isinstance(show.get("details"), dict) else {}
    model_info = show.get("model_info") if isinstance(show.get("model_info"), dict) else {}
    contexts = _context_lengths(model_info)
    return {
        "provider": "ollama",
        "model": model,
        "ollama_version": ollama_version,
        "installed": bool(tags) if tags_known else None,
        "digest": str(tags.get("digest")) if tags and tags.get("digest") else None,
        "size_bytes": int(tags["size"]) if tags and isinstance(tags.get("size"), (int, float)) else None,
        "details": {
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "quantization_level": details.get("quantization_level"),
            "format": details.get("format"),
        },
        "capabilities": {
            "completion": ("completion" in capabilities) if capabilities is not None else None,
            "tools": ("tools" in capabilities) if capabilities is not None else None,
            "vision": ("vision" in capabilities) if capabilities is not None else None,
            "thinking": thinking_supported,
        },
        "context": {
            "model_info_tokens": max(contexts) if contexts else None,
            "configured_num_ctx": _num_ctx(show.get("parameters")),
        },
        "runtime": {
            "loaded": bool(loaded) if runtime_known else None,
            "context_tokens": int(loaded["context_length"]) if loaded and isinstance(loaded.get("context_length"), (int, float)) else None,
            "size_bytes": int(loaded["size"]) if loaded and isinstance(loaded.get("size"), (int, float)) else None,
            "vram_bytes": int(loaded["size_vram"]) if loaded and isinstance(loaded.get("size_vram"), (int, float)) else None,
        },
        "issues": issues,
    }
