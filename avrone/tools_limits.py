"""limits_status + run_logs.

    limits_status {}
        Every load cap this node enforces, its current value and where it comes from (env or
        default), plus live usage where this node can see it: concurrent model calls in use,
        agents active, agents started per run. Read-only.

    run_logs {"prune"?: bool, "older_than_days"?: int, "keep"?: int}
        Run-log disk usage (count, bytes, oldest/newest). Run logs are append-only and never
        rotated, so a busy node grows data/runs forever. prune=true deletes run logs older than
        older_than_days (default 30, min 1) while always keeping the newest `keep` (default 50,
        min 10) and any run still active on this node. Acting when pruning.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext



def _limits() -> list[tuple[str, str, int, str]]:
    from . import ctxspawn, mcp_server, server
    rows = []
    for env, fn in (("AVRONE_MAX_BACKGROUND_RUNS", server.max_background_runs),
                    ("AVRONE_MAX_MESSAGE_CHARS", server.max_message_chars),
                    ("AVRONE_MCP_MAX_INFLIGHT", mcp_server.max_inflight),
                    ("AVRONE_MAX_AGENTS_PER_RUN", ctxspawn.max_agents_per_run),
                    ("AVRONE_SPAWN_CONTEXT_CHARS", ctxspawn.context_chars)):
        try:
            v = fn()
        except Exception:  # noqa: BLE001
            continue
        rows.append((env, "env" if os.environ.get(env, "").strip() else "default", v, ""))
    return rows


async def t_limits_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    core = getattr(ctx, "core", None)
    lines = ["limits_status:"]
    mp = getattr(getattr(core, "settings", None), "max_parallel", None)
    sem = getattr(core, "_sem", None)
    if isinstance(mp, int):
        free = getattr(sem, "_value", None)
        used = f", {mp - free} in use now" if isinstance(free, int) else ""
        lines.append(f"- AVRONE_MAX_PARALLEL (concurrent model calls) = {mp}{used}")
    for env, src, v, _ in _limits():
        lines.append(f"- {env} = {v} ({src})")
    from . import server
    bg = server.state.get("tasks")
    if isinstance(bg, set):
        lines.append(f"live: {len(bg)} background run(s) in flight of {server.max_background_runs()}")
    active = getattr(core, "active", None)
    if isinstance(active, dict):
        working = sum(1 for a in active.values() if isinstance(a, dict) and a.get("status") == "working")
        lines.append(f"live: {working} agent(s) working, {len(active)} known")
    ra = getattr(core, "run_agents", None)
    if isinstance(ra, dict) and ra:
        top = sorted(ra.items(), key=lambda kv: -kv[1])[:5]
        lines.append("agents started per run (top): " + ", ".join(f"{k}={v}" for k, v in top))
    return "\n".join(lines)


LIMITS_TOOLS = [
    Tool("limits_status", "read-only: every load cap this node enforces (background runs, message size, MCP "
         "in-flight, agents per run, model concurrency) with its value, source and live usage.",
         "{}", t_limits_status, source="builtin", schema={}),
]


def register_limits_tools(registry) -> None:
    for t in LIMITS_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
