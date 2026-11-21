"""Host configuration snapshot. Values shown from here are already masked."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .ollama_client import ollama_binary, ollama_host, probe_ollama
from .redact import mask_lines, mask_value


def _version(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    return text[0] if text else ""


def _git_config(repo: Path) -> list[dict[str, str]]:
    try:
        completed = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in (completed.stdout or "").splitlines():
        key, _, raw = line.partition("=")
        rows.append({"key": key, "value": mask_value(key, raw)})
    return rows


def _config_files(repo: Path) -> list[dict[str, Any]]:
    names = [".env", ".env.local", ".env.example", "pyproject.toml"]
    found = []
    for name in names:
        path = repo / name
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            found.append({"path": name, "error": str(exc), "text": ""})
            continue
        found.append({"path": name, "bytes": path.stat().st_size, "text": mask_lines(raw)[:4000]})
    return found


def capture_system(repo: Path) -> dict[str, Any]:
    """Read the host and repo configuration. Secret values are masked before return."""
    binary = ollama_binary()
    probe = probe_ollama()
    env = {key: mask_value(key, value) for key, value in os.environ.items()}
    interesting = {
        key: env[key]
        for key in sorted(env)
        if key.startswith(("AVRONE_", "CROW_", "OLLAMA_", "PYTHON", "PATH", "VIRTUAL_ENV"))
        or "TOKEN" in key
        or "KEY" in key
        or "SECRET" in key
    }
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "toolchain": {
            "python": _version([sys.executable, "--version"]),
            "git": _version(["git", "--version"]),
            "pytest": _version([sys.executable, "-m", "pytest", "--version"]),
            "node": _version(["node", "--version"]) if shutil.which("node") else "",
            "ollama": binary.get("version") or "",
        },
        "git_config": _git_config(repo),
        "config_files": _config_files(repo),
        "env": interesting,
        "env_count": len(env),
        "ollama": {
            "host": ollama_host(),
            "binary": binary,
            "probe": {key: probe.get(key) for key in ("state", "detail", "host", "error", "models")},
        },
        "inheritance": (
            "Test runs and agent worktrees inherit the host process environment. "
            "This panel stores only the masked view."
        ),
    }


def public_runtime() -> dict[str, str]:
    """Non-secret facts recorded on a round. Tokens are not copied."""
    binary = ollama_binary()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git": _version(["git", "--version"]),
        "ollama_host": ollama_host(),
        "ollama_binary": binary.get("path") or "",
        "ollama_version": binary.get("version") or "",
    }
