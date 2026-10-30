"""proc_status + api_surface: what an agent can see of its own host and its own node's front door.
Both read-only, own process tree / own app only, bounded output.

    proc_status {"all"?: bool}
        Child processes of this AVRONE process (from /proc; Linux only): pid, state, age, RSS and a
        truncated command line. Flags long-running children (possible orphans/runaways) past
        AVRONE_PROC_LONG_S (default 600). Never shows environment variables or other users' processes.

    api_surface {}
        This node's HTTP routes with their auth class (public, admin token, fleet-signed), plus
        whether admin and fleet are actually enabled right now. Lets a squad reason about what a
        peer or a browser can reach without probing it.
"""
from __future__ import annotations

import inspect
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

PROC = Path("/proc")
MAX_PROCS = 50
CMD_MAX = 160


def _long_s() -> float:
    try:
        v = float(os.getenv("AVRONE_PROC_LONG_S", "600") or 600)
    except ValueError:
        return 600.0
    return v if v > 0 else 600.0


def _boot_time() -> float | None:
    try:
        for line in (PROC / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _children(root_pid: int) -> dict[int, dict[str, Any]]:
    """pid -> info for every descendant of root_pid (bounded scan of /proc)."""
    info: dict[int, dict[str, Any]] = {}
    try:
        pids = [int(p.name) for p in PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return {}
    tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    btime = _boot_time()
    for pid in pids:
        try:
            stat = (PROC / str(pid) / "stat").read_text()
            rparen = stat.rindex(")")
            fields = stat[rparen + 2:].split()
            state, ppid, start_ticks, rss_pages = fields[0], int(fields[1]), int(fields[19]), int(fields[21])
            raw = (PROC / str(pid) / "cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            continue  # raced with exit, or not ours to read
        cmd = raw.replace(b"\x00", b" ").decode(errors="replace").strip() or stat[stat.index("(") + 1:rparen]
        started = btime + start_ticks / tck if btime is not None else None
        info[pid] = {"pid": pid, "ppid": ppid, "state": state, "started": started,
                     "rss_kb": rss_pages * (os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4),
                     "cmd": cmd[:CMD_MAX]}
    out: dict[int, dict[str, Any]] = {}
    frontier = {root_pid}
    while frontier:
        nxt = {p for p, i in info.items() if i["ppid"] in frontier and p not in out and p != root_pid}
        for p in nxt:
            out[p] = info[p]
        frontier = nxt
    return out


async def t_proc_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not PROC.is_dir():
        return "proc_status: /proc is not available on this host (Linux only)"
    kids = _children(os.getpid())
    now, long_s = time.time(), _long_s()
    rows = sorted(kids.values(), key=lambda i: i["started"] or 0)
    long_ = [r for r in rows if r["started"] is not None and now - r["started"] >= long_s]
    zombies = [r for r in rows if r["state"] == "Z"]
    show = rows if args.get("all") is True else (long_ + [z for z in zombies if z not in long_] or rows)
    lines = [f"proc_status: {len(rows)} child process(es) of pid {os.getpid()}; "
             f"{len(long_)} running >= {long_s:.0f}s, {len(zombies)} zombie(s)"]
    for r in show[:MAX_PROCS]:
        age = f"{now - r['started']:.0f}s" if r["started"] is not None else "?"
        lines.append(f"- {r['pid']} [{r['state']}] age {age} rss {r['rss_kb']}KB: {r['cmd']}")
    if len(show) > MAX_PROCS:
        lines.append(f"... {len(show) - MAX_PROCS} more")
    return "\n".join(lines)


def _auth_class(fn: Any) -> str:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return "unknown"
    if "_gateway(request" in src or "_verified(request" in src:
        return "fleet-signed"
    if "require_admin(" in src:
        return "admin"
    if "require_local(" in src or "caller_allowed(" in src:
        return "local"  # loopback or admin token, per AVRONE_PUBLIC_ACCESS
    return "public"


def api_surface() -> dict[str, Any]:
    from . import server  # lazy: the server imports the tool modules
    routes = []
    for r in server.app.routes:
        path = getattr(r, "path", "")
        if not path or path.startswith(("/openapi", "/docs", "/redoc", "/static")):
            continue
        methods = sorted(getattr(r, "methods", None) or {"WS"})
        methods = [m for m in methods if m != "HEAD"]
        routes.append({"path": path, "methods": methods, "auth": _auth_class(getattr(r, "endpoint", None))})
    return {"routes": sorted(routes, key=lambda x: x["path"]),
            "admin_enabled": bool(getattr(server.settings, "admin_token", None)),
            "access_policy": server.access_policy(),
            "fleet_enabled": bool(os.getenv("AVRONE_FLEET_TOKEN"))}


async def t_api_surface(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        a = api_surface()
    except Exception as e:  # noqa: BLE001 - a CLI-only node has no app to describe
        return f"api_surface: no HTTP app on this node ({type(e).__name__})"
    by: dict[str, int] = {}
    for r in a["routes"]:
        by[r["auth"]] = by.get(r["auth"], 0) + 1
    lines = [f"api_surface: {len(a['routes'])} route(s); " + ", ".join(f"{k}={v}" for k, v in sorted(by.items())),
             f"admin token {'SET' if a['admin_enabled'] else 'unset (admin routes answer 403)'}; "
             f"fleet {'ON' if a['fleet_enabled'] else 'off (signed routes refuse)'}; "
             f"AVRONE_PUBLIC_ACCESS={a.get('access_policy', '?')}"]
    for r in a["routes"]:
        lines.append(f"- {','.join(r['methods'])} {r['path']} [{r['auth']}]")
    return "\n".join(lines)


HOSTVIEW_TOOLS = [
    Tool("proc_status", "read-only: child processes of this AVRONE node (pid, state, age, RSS, command); "
         "flags long-running ones and zombies. Own process tree only, never env vars.",
         '{"all"?: bool}', t_proc_status, source="builtin", schema={"optional": {"all": "bool"}}),
    Tool("api_surface", "read-only: this node's HTTP routes and who can call each (public/admin/fleet-signed), "
         "and whether admin and fleet are enabled.", "{}", t_api_surface, source="builtin", schema={}),
]


def register_hostview_tools(registry) -> None:
    for t in HOSTVIEW_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
