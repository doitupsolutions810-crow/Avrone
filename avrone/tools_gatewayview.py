"""replay_guard + stream_status.

    replay_guard {}
        This node's fleet replay protection: live nonces in the cache vs its cap, whether the cache
        is persisted (data_dir/fleet_nonces.json) and readable, and whether the boot guard is active
        (the saved cache was unreadable at boot, so requests signed before boot are refused for two
        windows). Verdict OK / DEGRADED / OFF. Never prints nonces. Read-only.

    stream_status {}
        The live event stream (/ws/events): connected subscribers, each one's queue depth vs its
        1000-event limit, events emitted and events dropped because a subscriber was too slow.
        Read-only.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext


def replay_guard(fleet: Any) -> dict[str, Any]:
    from .fleet import NONCE_CACHE_MAX, WINDOW_S
    now = time.time()
    nonces = dict(getattr(fleet, "_nonces", {}) or {})
    live = sum(1 for v in nonces.values() if isinstance(v, (int, float)) and v >= now)
    path = getattr(fleet, "_nonce_path", None)
    persisted = readable = False
    if path is not None and path.exists():
        persisted = True
        try:
            readable = isinstance(json.loads(path.read_text()), dict)
        except (OSError, ValueError, RecursionError):
            readable = False
    guard = getattr(fleet, "_boot_guard", None)
    guard_left = max(0, int(guard + 2 * WINDOW_S - now)) if isinstance(guard, (int, float)) else 0
    return {"enabled": bool(getattr(fleet, "enabled", False)), "live": live, "cap": NONCE_CACHE_MAX,
            "persisted": persisted, "readable": readable, "has_path": path is not None,
            "boot_guard_s": guard_left, "window_s": WINDOW_S}


async def t_replay_guard(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = getattr(getattr(ctx, "core", None), "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "replay_guard: OFF - fleet disabled on this node (no AVRONE_FLEET_TOKEN), nothing to replay"
    r = replay_guard(f)
    problems = []
    if not r["has_path"]:
        problems.append("cache is memory-only: a restart forgets it")
    elif r["persisted"] and not r["readable"]:
        problems.append("saved cache unreadable: the next restart falls back to the boot guard")
    if r["live"] >= 0.9 * r["cap"]:
        problems.append("cache near its cap: new signed requests will be refused when full")
    head = "DEGRADED" if problems else "OK"
    lines = [f"replay_guard: {head} - {r['live']}/{r['cap']} live nonce(s), window {r['window_s']}s, "
             f"persisted={'yes' if r['persisted'] else 'not yet'}",
             f"boot guard: {'active, ' + str(r['boot_guard_s']) + 's left' if r['boot_guard_s'] else 'off'}"]
    lines += [f"- {p}" for p in problems]
    return "\n".join(lines)


GATEWAYVIEW_TOOLS = [
    Tool("replay_guard", "read-only: fleet replay protection state (live nonces vs cap, persisted cache, boot "
         "guard). Never prints nonces.", "{}", t_replay_guard, source="builtin", schema={}),
]


def register_gatewayview_tools(registry) -> None:
    for t in GATEWAYVIEW_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
