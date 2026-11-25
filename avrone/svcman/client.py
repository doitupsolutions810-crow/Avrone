"""Local client for the supervisor control API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import process


class ClientError(RuntimeError):
    pass


def api_endpoint(state_dir: Path) -> tuple[str, str]:
    info_path = Path(state_dir) / "api.json"
    token_path = Path(state_dir) / "token"
    if not info_path.is_file() or not token_path.is_file():
        raise ClientError("supervisor is not running")
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientError(f"cannot read {info_path}: {exc}") from exc
    pid = int(info.get("pid") or 0)
    if pid and not process.alive(pid):
        raise ClientError("supervisor is not running")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ClientError("supervisor token is empty")
    base = f"http://{info.get('host', '127.0.0.1')}:{int(info['port'])}"
    return base, token


def request(state_dir: Path, method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
    base, token = api_endpoint(state_dir)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ClientError(f"{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"supervisor API unreachable: {exc.reason}") from exc


def probe_status(state_dir: Path | None = None) -> dict:
    """Status for callers such as the Crow-OS workspace. Never raises."""
    from .supervisor import default_state_dir
    directory = Path(state_dir) if state_dir else default_state_dir()
    try:
        return {"state": "ok", **request(directory, "GET", "/v1/status", timeout=2.0)}
    except ClientError as exc:
        return {"state": "unreachable", "detail": str(exc), "state_dir": str(directory)}
    except Exception as exc:
        return {"state": "unreachable", "detail": str(exc), "state_dir": str(directory)}
