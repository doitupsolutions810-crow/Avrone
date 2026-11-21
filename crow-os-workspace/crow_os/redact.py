"""Mask token-like configuration before it is stored or shown."""

from __future__ import annotations

import re

SECRET_KEY = re.compile(
    r"token|secret|password|passwd|credential|api[-_]?key|authorization|cookie|private[-_]?key",
    re.I,
)
TOKEN_VALUE = re.compile(
    r"(ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|gho_[A-Za-z0-9]+|xox[baprs]-[A-Za-z0-9-]+|"
    r"sk-[A-Za-z0-9]|xai-[A-Za-z0-9]|AKIA[0-9A-Z]{8,}|-----BEGIN [A-Z ]+-----|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
USERINFO = re.compile(r"(https?://)[^/\s]+@")
MASK = "••••"


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY.search(key or ""))


def mask_text(value: str) -> str:
    text = USERINFO.sub(r"\1", value or "")
    if TOKEN_VALUE.search(text):
        return MASK
    return text


def mask_value(key: str, value: object) -> str:
    text = "" if value is None else str(value)
    if is_secret_key(key):
        return MASK if text else ""
    text = mask_text(text)
    if len(text) > 400:
        return text[:400] + "…"
    return text


def mask_env(env: dict[str, str]) -> dict[str, str]:
    return {key: mask_value(key, value) for key, value in sorted(env.items())}


def mask_lines(text: str) -> str:
    kept = []
    for line in (text or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, raw = line.partition("=")
            kept.append(f"{key}={mask_value(key.strip(), raw)}")
        else:
            kept.append(mask_text(line))
    return "\n".join(kept)
