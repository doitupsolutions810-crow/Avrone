"""Tool registry. Every tool an agent can call lives here: built-ins, plugins, MCP tools.

Contract:
    async def fn(ctx: AgentContext, args: dict) -> str
    registry.register(Tool(name, description, args_doc, fn, source="builtin"))
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .network_policy import safe_http_request

if TYPE_CHECKING:
    from .trinity import AgentContext

ToolFn = Callable[["AgentContext", dict[str, Any]], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    args_doc: str  # e.g. '{"cmd": str}'
    fn: ToolFn
    source: str = "builtin"  # builtin | plugin:<name> | mcp:<server>


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", tool.name):
            raise ValueError(f"bad tool name: {tool.name!r}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list(self) -> list[dict[str, str]]:
        return [{"name": t.name, "description": t.description, "args": t.args_doc, "source": t.source}
                for t in sorted(self._tools.values(), key=lambda t: t.name)]

    def docs(self) -> str:
        lines = ["Available tools (call exactly one per reply):"]
        for t in sorted(self._tools.values(), key=lambda t: t.name):
            lines.append(f"- {t.name} {t.args_doc}: {t.description}")
        return "\n".join(lines) + "\n"

    async def call(self, ctx: "AgentContext", name: str, args: dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'. Known: {', '.join(self.names())}"
        try:
            return await tool.fn(ctx, args)
        except Exception as e:  # errors go back to the agent
            return f"ERROR: {type(e).__name__}: {e}"


def _safe_path(root: Path, rel: str) -> Path:
    p = (root / str(rel).lstrip("/")).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("path escapes the workspace")
    return p


async def t_shell(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not ctx.core.settings.agents_can_shell:
        return "ERROR: shell is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    cmd = str(args.get("cmd", ""))
    res = await ctx.core.runner.run(cmd)
    if _INSTALL_RE.search(cmd):  # self-change: record it in the resource ledger
        try:
            from .agentkit import record_change
            record_change(ctx, "shell install", cmd[:200], res.exit_code == 0, (res.stdout or res.stderr)[-200:])
        except Exception:  # noqa: BLE001
            pass
    return res.text()


_INSTALL_RE = __import__("re").compile(
    r"\b(apt(-get)?\s+install|pip3?\s+install|python3?\s+-m\s+pip\s+install|npm\s+(i|install)|apk\s+add|yum\s+install|dnf\s+install|brew\s+install|cargo\s+install|go\s+install)\b")


async def t_read(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _safe_path(ctx.core.runner.workspace, str(args.get("path", "")))
    if not p.exists():
        return f"ERROR: {args.get('path')} not found"
    return p.read_text(errors="replace")[:12000]


async def t_write(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _safe_path(ctx.core.runner.workspace, str(args.get("path", "")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(args.get("content", "")))
    return f"wrote {p.relative_to(ctx.core.runner.workspace.resolve())} ({p.stat().st_size} bytes)"


async def t_fetch(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not ctx.core.settings.allow_network:
        return "ERROR: network access is disabled."
    url = str(args.get("url", ""))
    if not re.match(r"^https?://", url):
        return "ERROR: url must start with http(s)://"
    r = await safe_http_request("GET", url, headers={"User-Agent": "AVRONE/0.2"}, timeout=30)
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return f"[HTTP {r.status_code}] {text[:8000]}"


async def t_spawn(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if ctx.depth >= ctx.core.settings.max_spawn_depth:
        return "ERROR: max sub-agent depth reached; do this part yourself."
    return await ctx.core.run_agent(
        run_id=ctx.run_id, role=str(args.get("role", "Helper")), task=str(args.get("task", "")),
        parent=ctx.name, depth=ctx.depth + 1)


def builtin_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("shell", "run a real terminal command in the environment (install packages, run code, docker, git...).", '{"cmd": str}', t_shell))
    r.register(Tool("read_file", "read a file from the workspace.", '{"path": str}', t_read))
    r.register(Tool("write_file", "create or overwrite a file in the workspace.", '{"path": str, "content": str}', t_write))
    r.register(Tool("fetch", "HTTP GET a URL and return its text (first 8000 chars).", '{"url": str}', t_fetch))
    r.register(Tool("spawn", "spawn a sub-agent to handle a piece of your task; returns its result.", '{"role": str, "task": str}', t_spawn))
    return r
