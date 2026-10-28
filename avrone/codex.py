"""Eternal CodeX: the event bus and the permanent agent action log.

Every agent thought, tool call, spawn and result is an event. Events go to
live subscribers (the HUD over WebSocket) and are appended to a JSONL log per
run, so any run can be replayed or audited later.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Event:
    run_id: str
    kind: str  # run.start | agent.spawn | agent.think | tool.call | tool.result | agent.done | lead.* | run.done | error
    agent: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class Codex:
    def __init__(self, data_dir: Path):
        self.log_dir = data_dir / "runs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        # emit() is called from the event loop and from worker threads (runner, fleet);
        # one lock keeps each JSONL record a single whole line.
        self._write_lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def emit(self, run_id: str, kind: str, agent: str, **data: Any) -> Event:
        ev = Event(run_id=run_id, kind=kind, agent=agent, data=data)
        line = json.dumps(asdict(ev), default=str) + "\n"
        with self._write_lock, (self.log_dir / f"{run_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        return ev

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any] | None:
        """One JSONL record, or None for a blank, truncated, corrupt or non-object line."""
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return None
        return obj if isinstance(obj, dict) else None

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        stamped = []
        for p in self.log_dir.glob("*.jsonl"):
            try:  # a log can vanish between listing and stat (cleanup, concurrent delete)
                stamped.append((p.stat().st_mtime, p))
            except OSError:
                continue
        stamped.sort(key=lambda t: t[0], reverse=True)
        out = []
        for _, p in stamped[:limit]:
            try:
                with p.open(encoding="utf-8", errors="replace") as f:
                    start = self._parse_line(f.readline())
            except OSError:
                continue
            if start is None:
                continue
            data = start.get("data")
            prompt = data.get("prompt", "") if isinstance(data, dict) else ""
            out.append({"run_id": p.stem, "started": start.get("ts"), "prompt": prompt})
        return out

    def replay(self, run_id: str) -> list[dict[str, Any]]:
        """Every intact event of a run, in order. Truncated/corrupt lines (e.g. a crash mid-write)
        are skipped, so one bad line never hides the rest of the audit log."""
        p = self.log_dir / f"{run_id}.jsonl"
        if not p.exists() or not p.resolve().is_relative_to(self.log_dir.resolve()):
            return []
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [ev for ev in map(self._parse_line, text.splitlines()) if ev is not None]
