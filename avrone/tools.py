"""Tool registry. Every tool an agent can call lives here: built-ins, plugins, MCP tools.

Contract:
    async def fn(ctx: AgentContext, args: dict) -> str
    registry.register(Tool(name, description, args_doc, fn, source="builtin"))
"""
from __future__ import annotations

import difflib
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

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
    # Optional arg schema, checked before the call so a bad call never runs:
    #   {"required": {"path": "str"}, "optional": {"limit": "int"}}
    # Types: str | int | float | bool | list | dict, or alternatives joined by "|"
    # ("str|dict"). Unknown keys are allowed. None = no validation (plugins, MCP...).
    schema: dict[str, dict[str, str]] | None = None


_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "float": (int, float), "bool": (bool,),
    "list": (list,), "dict": (dict,), "any": (object,),
}

# Schemas for the core tools. Attached at register() time to builtin tools that don't
# declare their own, so the tool tables in tools_extra.py etc. stay untouched.
CORE_SCHEMAS: dict[str, dict[str, dict[str, str]]] = {
    "shell": {"required": {"cmd": "str"}},
    "python": {"required": {"code": "str"}},
    "read_file": {"required": {"path": "str"}},
    "write_file": {"required": {"path": "str", "content": "str"}},
    "edit_file": {"required": {"path": "str", "old": "str", "new": "str"}},
    "fetch": {"required": {"url": "str"}},
    "web_search": {"required": {"query": "str"}, "optional": {"max_results": "int"}},
    "spawn": {"required": {"task": "str"}, "optional": {"role": "str", "context": "str", "focus": "str",
                                                       "share_dir": "bool"}},
    "list_files": {"optional": {"path": "str", "depth": "int"}},
    "search_files": {"required": {"pattern": "str"}, "optional": {"path": "str"}},
    "http_request": {"required": {"method": "str", "url": "str"},
                     "optional": {"headers": "dict", "body": "str|dict"}},
    "git": {"required": {"args": "str"}},
    "think": {"required": {"thought": "str"}},
}


def _type_ok(value: Any, spec: str) -> bool:
    for name in str(spec).split("|"):
        types = _TYPES.get(name.strip())
        if types is None:
            return True  # unknown type word in a schema: don't block the call on it
        if isinstance(value, bool) and name.strip() in ("int", "float"):
            continue  # True is an int in Python, never in a tool schema
        if isinstance(value, types):
            return True
    return False


_DOC_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"(\?)?\s*:\s*(str|int|float|bool)\b(?!\s*\|)')


def infer_schema(args_doc: str) -> dict[str, dict[str, str]] | None:
    """A conservative schema from a builtin tool's args_doc: only top-level REQUIRED keys of a plain
    scalar type become required (so a missing "host" is an ARG ERROR before the tool runs). Optional
    keys are left to the tool body (models often send "5" for an int). Nothing to require gives {}
    (an object is still required); docs with alternatives ("... or {...}") give None (no pre-check)."""
    doc = str(args_doc or "").strip()
    if not doc.startswith("{") or " or {" in doc:
        return None
    depth, top = 0, []
    for ch in doc:  # keep only depth-1 text so nested {"path": str} inside lists is ignored
        if ch in "{[":
            depth += 1
            top.append(" " if depth > 1 else ch)
            continue
        if ch in "}]":
            depth -= 1
            top.append(" ")
            continue
        top.append(ch if depth == 1 else " ")
    req = {k: t for k, opt, t in _DOC_KEY_RE.findall("".join(top)) if not opt}
    return {"required": req} if req else {}


def validate_args(schema: dict[str, dict[str, str]] | None, args: Any) -> list[str]:
    """Problems with `args` against `schema` (empty list = fine)."""
    if schema is None:
        return []
    if not isinstance(args, dict):  # {} still means "an object, any keys"
        return [f"args must be a JSON object, got {type(args).__name__}"]
    if not schema:
        return []
    problems: list[str] = []
    for key, spec in (schema.get("required") or {}).items():
        if key not in args or args[key] is None:
            problems.append(f"missing '{key}'")
        elif not _type_ok(args[key], spec):
            problems.append(f"'{key}' must be {spec}, got {type(args[key]).__name__}")
    for key, spec in (schema.get("optional") or {}).items():
        if args.get(key) is not None and not _type_ok(args[key], spec):
            problems.append(f"'{key}' must be {spec}, got {type(args[key]).__name__}")
    return problems


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", tool.name):
            raise ValueError(f"bad tool name: {tool.name!r}")
        if tool.schema is None and tool.source == "builtin" and tool.name in CORE_SCHEMAS:
            tool.schema = CORE_SCHEMAS[tool.name]
        if tool.schema is None and tool.source == "builtin":
            tool.schema = infer_schema(tool.args_doc)
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
        lines = ["Available tools (one call per reply, or a small batch of independent calls):"]
        for t in sorted(self._tools.values(), key=lambda t: t.name):
            lines.append(f"- {t.name} {t.args_doc}: {t.description}")
        return "\n".join(lines) + "\n"

    def suggest(self, name: str, n: int = 3) -> list[str]:
        """Registered tool names closest to `name` (for typos like 'shel' or 'readfile')."""
        names = self.names()
        lowered = {t.lower(): t for t in names}
        hits = difflib.get_close_matches(str(name).lower(), list(lowered), n=n, cutoff=0.6)
        return [lowered[h] for h in hits]

    def unknown_message(self, name: str) -> str:
        close = self.suggest(name)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"ERROR: unknown tool '{name}'.{hint} Known: {', '.join(self.names())}"

    def check(self, name: str, args: Any) -> tuple[str, str] | None:
        """Pre-call check: None if the call may run, else (reason, message for the model).

        reason is "unknown_tool" or "bad_args"; the tool is NOT run in either case."""
        tool = self.get(name)
        if tool is None:
            return "unknown_tool", self.unknown_message(name)
        problems = validate_args(tool.schema, args)
        if problems:
            return "bad_args", (f"ARG ERROR ({name}): {'; '.join(problems)}; expected {tool.args_doc}. "
                                "The tool did not run; resend the call with fixed args.")
        return None

    async def call(self, ctx: "AgentContext", name: str, args: dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return self.unknown_message(name)
        if args is None:
            args = {}
        # Defense in depth for entry points that skip check() (MCP, fleet, admin, direct calls):
        # a tool body never sees a list/str where it expects an object. (Typed per-field checks stay
        # in check() and in each tool, which give the model their own, more specific messages.)
        if not isinstance(args, dict):
            return (f"ARG ERROR ({name}): args must be a JSON object, got {type(args).__name__}; "
                    f"expected {tool.args_doc}. The tool did not run.")
        try:
            out = await tool.fn(ctx, args)
        except Exception as e:  # errors go back to the agent
            return f"ERROR: {type(e).__name__}: {e}"
        return out if isinstance(out, str) else str(out)


def _safe_path(root: Path, rel: str) -> Path:
    p = (root / str(rel).lstrip("/")).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("path escapes the workspace")
    return p


async def run_for_agent(ctx: "AgentContext", cmd: str, timeout: int | None = None):
    """Run `cmd` for this agent: in its own persistent dir when it has one, else as before.

    Runners that predate per-agent cwd (no `cwd` parameter) keep their old behavior."""
    runner = ctx.core.runner
    agent_dir = getattr(ctx, "agent_dir", None)
    if agent_dir is not None and runner_accepts_cwd(runner):
        return await runner.run(cmd, timeout=timeout, cwd=agent_dir)
    return await runner.run(cmd, timeout=timeout) if timeout else await runner.run(cmd)


def runner_accepts_cwd(runner: Any) -> bool:
    """True if `runner.run` takes a per-agent `cwd` (runners predating v0.16 do not)."""
    try:
        return "cwd" in inspect.signature(runner.run).parameters
    except (TypeError, ValueError):
        return False


async def t_shell(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not ctx.core.settings.agents_can_shell:
        return "ERROR: shell is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    cmd = str(args.get("cmd", ""))
    res = await run_for_agent(ctx, cmd)
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
    from . import fsunify
    p = fsunify.resolve(ctx, args.get("path", ""), "read_file").path
    if not p.exists():
        return f"ERROR: {args.get('path')} not found"
    return p.read_text(errors="replace")[:12000]


async def t_write(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from . import fsunify
    r = fsunify.resolve(ctx, args.get("path", ""), "write_file")
    p = r.path
    if r.in_agent:
        from .agentfs_tools import quota_write
        size, used, quota = quota_write(ctx, r.root, p, str(args.get("content", "")).encode("utf-8"),
                                        tool="write_file")
        return f"wrote {fsunify.display(ctx, p)} ({size} bytes; agent dir {used}/{quota} bytes)"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(args.get("content", "")))
    if r.unified:
        return f"wrote {fsunify.display(ctx, p)} ({p.stat().st_size} bytes)"
    return f"wrote {p.relative_to(ctx.core.runner.workspace.resolve())} ({p.stat().st_size} bytes)"


async def t_fetch(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not ctx.core.settings.allow_network:
        return "ERROR: network access is disabled."
    url = str(args.get("url", ""))
    if not re.match(r"^https?://", url):
        return "ERROR: url must start with http(s)://"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "AVRONE/0.2"})
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return f"[HTTP {r.status_code}] {text[:8000]}"


async def t_spawn(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if ctx.depth >= ctx.core.settings.max_spawn_depth:
        return "ERROR: max sub-agent depth reached; do this part yourself."
    from .ctxspawn import spawn  # ctxspawn: context pack, lineage, agent cap, share_dir
    return await spawn(ctx, args)


def builtin_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("shell", "run a real terminal command in the environment (install packages, run code, docker, git...). Runs in your own persistent agent dir ($AVRONE_AGENT_DIR) when you have one; the shared workspace is $AVRONE_SHARED_WORKSPACE.", '{"cmd": str}', t_shell))
    r.register(Tool("read_file", "read a file. With your own agent dir, paths are relative to it; prefix `shared/` (e.g. shared/notes.md) to reach the shared team workspace. No absolute paths or '..'.", '{"path": str}', t_read))
    r.register(Tool("write_file", "create or overwrite a file. Relative paths land in your own persistent agent dir (quota applies); prefix `shared/` to write to the shared team workspace.", '{"path": str, "content": str}', t_write))
    r.register(Tool("fetch", "HTTP GET a URL and return its text (first 8000 chars).", '{"url": str}', t_fetch))
    r.register(Tool("spawn", "spawn a sub-agent to handle a piece of your task; returns its result. context: full (default: it sees the run task, lineage, your recent tool calls, memory and file names) | summary | none; share_dir=true lets it read (not write) your agent dir as parent/.", '{"role": str, "task": str, "context"?: str, "focus"?: str, "share_dir"?: bool}', t_spawn))
    return r
