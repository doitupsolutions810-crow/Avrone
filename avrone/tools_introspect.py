"""Read-only introspection tools: let an agent look at its own tooling and settings.

    inspect_tool {"name"?: str, "query"?: str}
        name  -> full description, args_doc, source, arg schema and defining module of ONE tool
        query -> bounded list of tools whose name/description contains the substring

Nothing here writes, spawns, runs a command or touches the network. Output is capped.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

INSPECT_MAX_MATCHES = 50
INSPECT_MAX_QUERY = 200
INSPECT_MAX_DESC = 4000
INSPECT_MAX_CHARS = 8000
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_\-.]{1,128}")


def _registry(ctx: "AgentContext") -> Any:
    reg = getattr(ctx.core, "tools", None)
    if reg is None or not callable(getattr(reg, "get", None)) or not callable(getattr(reg, "names", None)):
        return None
    return reg


def _cap(text: str, limit: int = INSPECT_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _schema_lines(schema: Any) -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return ["schema: (none; args are not pre-validated)"]
    out = []
    for part in ("required", "optional"):
        spec = schema.get(part) or {}
        if isinstance(spec, dict) and spec:
            out.append(f"{part}: " + ", ".join(f"{k}: {v}" for k, v in spec.items()))
    return out or ["schema: (empty)"]


async def t_inspect_tool(ctx: "AgentContext", args: dict[str, Any]) -> str:
    reg = _registry(ctx)
    if reg is None:
        return "ERROR: the tool registry is not available"
    name = str(args.get("name") or "").strip()
    query = str(args.get("query") or "").strip()
    if bool(name) == bool(query):
        return "ERROR: give exactly one of name or query"
    if name:
        if not _TOOL_NAME_RE.fullmatch(name):
            return "ERROR: invalid tool name"
        tool = reg.get(name)
        if tool is None:
            close = reg.suggest(name) if callable(getattr(reg, "suggest", None)) else []
            return f"ERROR: unknown tool {name!r}." + (f" Did you mean: {', '.join(close)}?" if close else "")
        desc = str(tool.description)
        if len(desc) > INSPECT_MAX_DESC:
            desc = desc[:INSPECT_MAX_DESC] + "..."
        module = getattr(getattr(tool, "fn", None), "__module__", None) or "unknown"
        lines = [f"tool: {tool.name}", f"source: {tool.source}", f"module: {module}",
                 f"args: {tool.args_doc}", *_schema_lines(getattr(tool, "schema", None)),
                 f"description: {desc}"]
        return _cap("\n".join(lines))
    if len(query) > INSPECT_MAX_QUERY:
        return f"ERROR: query is longer than {INSPECT_MAX_QUERY} chars"
    q = query.lower()
    hits = []
    for n in reg.names():
        t = reg.get(n)
        if t is None:
            continue
        if q in t.name.lower() or q in str(t.description).lower():
            hits.append(t)
    if not hits:
        return f"No tools match {query!r} ({len(reg.names())} registered)."
    lines = [f"{len(hits)} tool(s) match {query!r}; showing {min(len(hits), INSPECT_MAX_MATCHES)}:"]
    for t in hits[:INSPECT_MAX_MATCHES]:
        d = re.sub(r"\s+", " ", str(t.description))
        lines.append(f"- {t.name} [{t.source}] {t.args_doc}: {d[:120]}{'...' if len(d) > 120 else ''}")
    if len(hits) > INSPECT_MAX_MATCHES:
        lines.append(f"...[{len(hits) - INSPECT_MAX_MATCHES} more; narrow the query]")
    return _cap("\n".join(lines))


INTROSPECT_TOOLS: list[Tool] = [
    Tool("inspect_tool",
         "read-only: show ONE registered tool's full description, args, arg schema, source and module "
         "(name), or list tools whose name/description contains a substring (query, max 50).",
         '{"name"?: str, "query"?: str}', t_inspect_tool, source="builtin",
         schema={"optional": {"name": "str", "query": "str"}}),
]


def register_introspect_tools(registry) -> None:
    for t in INTROSPECT_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
