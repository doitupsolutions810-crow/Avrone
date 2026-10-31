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


def _redact_on() -> bool:
    import os
    return (os.getenv("AVRONE_CODEX_REDACT") or "on").strip().lower() not in ("off", "0", "false", "no")


def _scrub(v: Any, depth: int = 0) -> Any:
    """Mask secrets (env secret values, tokens, keys, URL credentials) in every string an event carries,
    before it is written to the run log or sent to /ws/events. Never raises; deep junk is stringified."""
    from .ctxspawn import redact
    if isinstance(v, str):
        return redact(v) if len(v) >= 6 else v
    if depth > 8:
        return redact(v) if not isinstance(v, (int, float, bool, type(None))) else v
    if isinstance(v, dict):
        return {k: _scrub(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x, depth + 1) for x in v]
    return v


DEFAULT_MAX_SUBSCRIBERS, HARD_MAX_SUBSCRIBERS = 64, 1024


def max_subscribers() -> int:
    """Live event-stream subscribers allowed at once (AVRONE_MAX_WS_SUBSCRIBERS)."""
    import os
    try:
        n = int(os.environ.get("AVRONE_MAX_WS_SUBSCRIBERS", "").strip() or DEFAULT_MAX_SUBSCRIBERS)
    except ValueError:
        n = DEFAULT_MAX_SUBSCRIBERS
    return max(1, min(HARD_MAX_SUBSCRIBERS, n))


class SubscribersFull(RuntimeError):
    pass


class Codex:
    def __init__(self, data_dir: Path):
        self.log_dir = data_dir / "runs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        # the event loop each subscriber queue belongs to: asyncio.Queue is not thread-safe, and a
        # put from a worker thread never wakes the loop, so a waiting dashboard stalled until some
        # unrelated loop activity (measured: a thread emit arrived only when a 3 s timer fired)
        self._loops: dict[asyncio.Queue[Event], asyncio.AbstractEventLoop] = {}
        # emit() is called from the event loop and from worker threads (runner, fleet);
        # one lock keeps each JSONL record a single whole line.
        self._write_lock = threading.Lock()
        self.dropped = 0  # events a full subscriber queue could not take (slow dashboard)
        self.emitted = 0

    def subscribe(self) -> asyncio.Queue[Event]:
        # every emit fans out to every subscriber, and each queue holds up to 1000 events: unbounded
        # subscribers made each run's events cost O(subscribers) and memory grow per connection
        if len(self._subscribers) >= max_subscribers():
            raise SubscribersFull(f"event stream full ({max_subscribers()} subscribers; AVRONE_MAX_WS_SUBSCRIBERS)")
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        try:
            self._loops[q] = asyncio.get_running_loop()
        except RuntimeError:
            pass  # subscribed outside a loop: delivered by direct put, as before
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)
        self._loops.pop(q, None)

    def _offer(self, q: asyncio.Queue[Event], ev: Event) -> None:
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            self.dropped += 1

    def _deliver(self, q: asyncio.Queue[Event], ev: Event) -> None:
        loop = self._loops.get(q)
        if loop is None:
            return self._offer(q, ev)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return self._offer(q, ev)
        if loop.is_closed():
            self.unsubscribe(q)
            return None
        try:
            loop.call_soon_threadsafe(self._offer, q, ev)
        except RuntimeError:  # loop closed between the check and the call
            self.unsubscribe(q)

    def emit(self, run_id: str, kind: str, agent: str, **data: Any) -> Event:
        if _redact_on():
            data = _scrub(data)
        ev = Event(run_id=run_id, kind=kind, agent=agent, data=data)
        line = json.dumps(asdict(ev), default=str) + "\n"
        with self._write_lock, (self.log_dir / f"{run_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
        self.emitted += 1
        for q in list(self._subscribers):
            self._deliver(q, ev)
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
