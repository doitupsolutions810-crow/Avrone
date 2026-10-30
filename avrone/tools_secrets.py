"""secret_scan + mcp_selftest.

    secret_scan {"scope"?: "runs"|"data"|"workspace", "fix"?: bool}
        Find secret-shaped strings (env secret values, bearer/API keys, private keys, URL credentials)
        in this node's own files: run logs (default), the whole data dir, or the workspace. Reports
        path, line and kind only - never the value. fix=true (runs scope only) rewrites affected run
        logs with the same redaction codex now applies on write (atomic, per file). Bounded: at most
        MAX_FILES files and MAX_BYTES per file are read.

    mcp_selftest {}
        Drive this node's MCP server in-process (no subprocess, no network): initialize, ping,
        tools/list, and three malformed requests that must come back as JSON-RPC errors. Reports
        the protocol version, the advertised tools and any check that failed.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

MAX_FILES = 2000
MAX_BYTES = 2 * 1024 * 1024
MAX_SHOWN = 30
TEXT_SUFFIXES = {".jsonl", ".json", ".log", ".txt", ".md", ".py", ".sh", ".env", ".yml", ".yaml", ".toml", ".cfg",
                 ".ini", ""}


def _kinds(line: str) -> list[str]:
    from . import ctxspawn
    found = []
    try:
        for v in ctxspawn._env_secret_values():
            if v in line:
                found.append("env-secret-value")
                break
    except Exception:  # noqa: BLE001
        pass
    names = ["private-key", "auth-header", "bearer", "secret-assignment", "api-key", "github-token", "slack-token",
             "aws-key", "google-key", "jwt", "url-credentials"]
    for (pat, _rep), name in zip(ctxspawn._PATTERNS, names):
        if pat.search(line):
            found.append(name)
    return found


def _roots(ctx: "AgentContext", scope: str) -> Path | None:
    core = getattr(ctx, "core", None)
    settings = getattr(core, "settings", None)
    if scope == "workspace":
        ws = getattr(getattr(core, "runner", None), "workspace", None) or getattr(settings, "workspace", None)
        return Path(ws) if ws else None
    dd = getattr(settings, "data_dir", None)
    if dd is None:
        return None
    return Path(dd) / "runs" if scope == "runs" else Path(dd)


def scan(root: Path) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    files = scanned = 0
    for p in sorted(root.rglob("*")) if root.is_dir() else []:
        if files >= MAX_FILES:
            break
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in TEXT_SUFFIXES or ".git" in p.parts:
            continue
        files += 1
        try:
            with p.open("rb") as f:
                data = f.read(MAX_BYTES)
        except OSError:
            continue
        scanned += 1
        for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            k = _kinds(line)
            if k:
                hits.append({"path": str(p.relative_to(root)), "line": i, "kinds": sorted(set(k))})
    return {"files": scanned, "hits": hits, "truncated": files >= MAX_FILES}


def fix_runs(root: Path, paths: set[str]) -> int:
    from .ctxspawn import redact
    fixed = 0
    for rel in sorted(paths):
        p = root / rel
        if p.is_symlink() or not p.is_file() or p.suffix != ".jsonl":
            continue
        try:
            text = p.read_text(errors="replace")
            new = "\n".join(redact(ln) if _kinds(ln) else ln for ln in text.split("\n"))
            if new == text:
                continue
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new)
                os.replace(tmp, p)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            fixed += 1
        except OSError:
            continue
    return fixed


async def t_secret_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    scope = args.get("scope", "runs")
    if scope not in ("runs", "data", "workspace"):
        return "ERROR: scope must be runs, data or workspace"
    if args.get("fix") is True and scope != "runs":
        return "ERROR: fix is only allowed for scope runs (run logs); fix other files by hand"
    root = _roots(ctx, scope)
    if root is None:
        return f"secret_scan: no {scope} directory configured on this node"
    res = await asyncio.to_thread(scan, root)
    hits = res["hits"]
    head = (f"secret_scan [{scope}]: {'CLEAN' if not hits else 'FOUND'} - {len(hits)} line(s) in "
            f"{len({h['path'] for h in hits})} file(s); {res['files']} file(s) read"
            + (" (file cap reached)" if res["truncated"] else ""))
    lines = [head] + [f"- {h['path']}:{h['line']} {', '.join(h['kinds'])}" for h in hits[:MAX_SHOWN]]
    if len(hits) > MAX_SHOWN:
        lines.append(f"... {len(hits) - MAX_SHOWN} more")
    if hits and args.get("fix") is True:
        n = await asyncio.to_thread(fix_runs, root, {h["path"] for h in hits})
        after = await asyncio.to_thread(scan, root)
        lines.append(f"fixed {n} run log(s); {len(after['hits'])} line(s) still flagged")
    return "\n".join(lines)


SECRET_TOOLS = [
    Tool("secret_scan", "read-only by default: find secret-shaped strings in this node's run logs, data dir or "
         "workspace (path:line and kind, never the value); fix=true redacts run logs in place.",
         '{"scope"?: "runs"|"data"|"workspace", "fix"?: bool}', t_secret_scan, source="builtin",
         schema={"optional": {"scope": "str", "fix": "bool"}}),
]


def register_secret_tools(registry) -> None:
    for t in SECRET_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
