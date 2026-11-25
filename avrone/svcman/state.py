"""Durable supervisor state and the single-instance lock."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path


class StateError(RuntimeError):
    pass


class InstanceLock:
    """Exclusive flock held for the life of the supervisor process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise StateError(f"another supervisor holds {self.path}") from exc

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class StateStore:
    """Atomic JSON state. Readers never observe a partial write."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.data: dict = {"version": 1, "services": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"cannot read state {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
            raise StateError(f"state {self.path} is missing a services object")
        self.data = raw
        self.data.setdefault("version", 1)

    def service(self, name: str) -> dict:
        with self._lock:
            current = self.data["services"].get(name)
            return dict(current) if isinstance(current, dict) else {}

    def services(self) -> dict[str, dict]:
        with self._lock:
            return {name: dict(body) for name, body in self.data["services"].items() if isinstance(body, dict)}

    def update_service(self, name: str, **fields) -> None:
        with self._lock:
            body = self.data["services"].setdefault(name, {})
            body.update(fields)
            self._save_locked()

    def replace_services(self, services: dict[str, dict]) -> None:
        with self._lock:
            self.data["services"] = services
            self._save_locked()

    def _save_locked(self) -> None:
        payload = json.dumps(self.data, indent=2, sort_keys=True).encode("utf-8")
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
