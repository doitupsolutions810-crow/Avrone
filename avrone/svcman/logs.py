"""Per-service log capture with rotation, plus the structured event log."""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path

from .timeutil import iso_now


class RotatingLog:
    """Append-only byte log. Rotates before a write that would pass `max_bytes`."""

    def __init__(self, path: Path, max_bytes: int, backups: int):
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "ab", buffering=0)
        self.size = self.path.stat().st_size if self.path.exists() else 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self.size > 0 and self.size + len(data) > self.max_bytes:
                self._rotate()
            self._fh.write(data)
            self.size += len(data)

    def _rotate(self) -> None:
        self._fh.close()
        oldest = self._backup(self.backups)
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backups - 1, 0, -1):
            src = self._backup(index)
            if src.exists():
                os.replace(src, self._backup(index + 1))
        os.replace(self.path, self._backup(1))
        self._fh = open(self.path, "ab", buffering=0)
        self.size = 0

    def _backup(self, index: int) -> Path:
        return self.path.with_name(self.path.name + f".{index}")

    def tail_lines(self, count: int) -> list[str]:
        files = [self._backup(index) for index in range(self.backups, 0, -1)]
        files.append(self.path)
        chunks = []
        for file in files:
            if file.exists():
                chunks.append(file.read_bytes())
        text = b"".join(chunks).decode("utf-8", "replace")
        lines = text.splitlines()
        if count <= 0:
            return lines
        return lines[-count:]

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


class EventLog:
    """JSONL event log. Every record has an ISO-8601 UTC timestamp.

    Crash traces are stored in the `trace` field and are not stripped when the
    file rotates; rotation keeps whole lines and the configured backups.
    """

    def __init__(self, path: Path, max_bytes: int = 20_971_520, backups: int = 5):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotating = RotatingLog(self.path, max_bytes, backups)
        self._lock = threading.Lock()
        self.recent: deque[dict] = deque(maxlen=2000)

    def emit(self, event: str, **fields) -> dict:
        record = {"ts": iso_now(), "event": event}
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            self._rotating.write(data)
            self.recent.append(record)
        if event in {"crash", "circuit_open", "anomaly"}:
            self._fsync()
        return record

    def _fsync(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def tail(self, count: int = 200) -> list[dict]:
        with self._lock:
            items = list(self.recent)
        if count <= 0:
            return items
        return items[-count:]

    def close(self) -> None:
        self._rotating.close()
