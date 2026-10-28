"""Eternal CodeX: the event bus and the permanent agent action log.

Every agent thought, tool call, spawn and result is an event. Events go to
live subscribers (the HUD over WebSocket) and are appended to a JSONL log per
run, so any run can be replayed or audited later.
"""
from __future__ import annotations

import asyncio
import json
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

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def emit(self, run_id: str, kind: str, agent: str, **data: Any) -> Event:
        ev = Event(run_id=run_id, kind=kind, agent=agent, data=data)
        with (self.log_dir / f"{run_id}.jsonl").open("a") as f:
            f.write(json.dumps(asdict(ev), default=str) + "\n")
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        return ev

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        files = sorted(self.log_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:limit]:
            first = p.open().readline()
            try:
                start = json.loads(first)
            except json.JSONDecodeError:
                continue
            out.append({"run_id": p.stem, "started": start.get("ts"), "prompt": start.get("data", {}).get("prompt", "")})
        return out

    def replay(self, run_id: str) -> list[dict[str, Any]]:
        p = self.log_dir / f"{run_id}.jsonl"
        if not p.exists() or not p.resolve().is_relative_to(self.log_dir.resolve()):
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
