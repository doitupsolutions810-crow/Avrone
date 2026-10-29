"""Read-only introspection tools: let an agent look at its own tooling and settings.

    inspect_tool {"name"?: str, "query"?: str}
        name  -> full description, args_doc, source, arg schema and defining module of ONE tool
        query -> bounded list of tools whose name/description contains the substring
    env_report {"prefix"?: str, "format"?: "text"|"json"}
        effective Settings fields + AVRONE_* environment variables, REDACTED: secret-shaped
        names (TOKEN/KEY/SECRET/PASSWORD/...) report presence only, URL credentials and
        secret query params are masked, and token-shaped values are hidden whatever their name.

Nothing here writes, spawns, runs a command or touches the network. Output is capped.
"""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool, runner_accepts_cwd

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


# ---------------------------------------------------------------------- runtime_preflight
def _agent_container_preflight(ctx: "AgentContext", runner: Any, runner_kind: str) -> dict[str, Any]:
    """Describe per-agent container mode without exposing a path or spawning a process."""
    try:
        from . import agentcontainer, agentfs
        mode = agentcontainer.container_mode()
        agent_id = getattr(ctx, "agent_id", None)
        pool = getattr(runner, "agent_pool", None)
        homes = getattr(pool, "homes", None) if pool is not None else None
        node = getattr(pool, "node", None) if pool is not None else None
        live = False
        if agent_id and isinstance(homes, dict):
            node_name = node if isinstance(node, str) and node else agentcontainer.local_node_id()
            home_name = agentcontainer.container_name(node_name, agentfs.sanitize_agent_id(agent_id))
            live = home_name in homes
        return {
            "mode": mode,
            "persistent_configured": mode == "persistent" and runner_kind == "docker",
            "agent_id_present": bool(agent_id),
            "live_container_for_agent": live,
        }
    except Exception:  # noqa: BLE001 - diagnostics must never break the main run
        return {"mode": "unknown", "hosted_for_agent": False,
                "agent_id_present": bool(getattr(ctx, "agent_id", None)),
                "live_container_for_agent": False}



PREFLIGHT_MAX_CHARS = 4000


def runtime_preflight(ctx: "AgentContext") -> dict[str, Any]:
    """Return a bounded, read-only snapshot of local execution prerequisites."""
    core = getattr(ctx, "core", None)
    settings = getattr(core, "settings", None)
    runner = getattr(core, "runner", None)
    raw_workspace = getattr(runner, "workspace", None)
    if raw_workspace is None:
        raw_workspace = getattr(settings, "workspace", None)

    configured = raw_workspace is not None
    exists = False
    writable = False
    if configured:
        try:
            workspace = Path(str(raw_workspace)).expanduser()
            exists = workspace.is_dir()
            writable = exists and os.access(workspace, os.W_OK)
        except (OSError, RuntimeError, TypeError, ValueError):
            exists = writable = False

    kind = str(getattr(runner, "kind", "unknown") or "unknown").lower()
    docker_available = bool(shutil.which("docker"))
    python_ok = sys.version_info >= (3, 11)
    checks = {
        "python_311_plus": python_ok,
        "workspace_exists": exists,
        "workspace_writable": writable,
        "runner_known": kind in {"local", "docker"},
        "docker_available": docker_available if kind == "docker" else None,
    }
    ready = bool(python_ok and exists and writable and checks["runner_known"]
                 and (kind != "docker" or docker_available))
    return {
        "ready": ready,
        "python": platform.python_version(),
        "runner": kind,
        "workspace": {"configured": configured, "exists": exists, "writable": writable},
        "docker": {"available": docker_available},
        "model": {
            "provider": str(getattr(settings, "provider", "unknown")),
            "mode": str(getattr(settings, "llm_mode", "unknown")),
            "base_url_set": bool(getattr(settings, "llm_base_url", "")),
            "api_key_set": bool(getattr(settings, "llm_api_key", "")),
        },
        "admin": {"enabled": bool(getattr(settings, "admin_token", ""))},
        "agent_container": _agent_container_preflight(ctx, runner, kind),
        "checks": checks,
    }


async def t_runtime_preflight(ctx: "AgentContext", args: dict[str, Any]) -> str:
    fmt = str(args.get("format", "text")).lower()
    if fmt not in {"text", "json"}:
        return 'ERROR: format must be "text" or "json"'
    rep = runtime_preflight(ctx)
    if fmt == "json":
        return _cap(json.dumps(rep, indent=2, sort_keys=True), PREFLIGHT_MAX_CHARS)
    lines = [
        f"runtime_preflight (read-only; ready={'yes' if rep['ready'] else 'no'})",
        f"python: {rep['python']}",
        f"runner: {rep['runner']}",
        "workspace: " + ("configured" if rep["workspace"]["configured"] else "not configured")
        + f"; exists={'yes' if rep['workspace']['exists'] else 'no'}"
        + f"; writable={'yes' if rep['workspace']['writable'] else 'no'}",
        f"docker available: {'yes' if rep['docker']['available'] else 'no'}",
        f"model: provider={rep['model']['provider']} mode={rep['model']['mode']}"
        + f" base_url={'set' if rep['model']['base_url_set'] else 'unset'}"
        + f" api_key={'set' if rep['model']['api_key_set'] else 'unset'}",
        f"admin: {'enabled' if rep['admin']['enabled'] else 'disabled'}",
    ]
    return _cap("\n".join(lines), PREFLIGHT_MAX_CHARS)


async def t_node_runtime(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Probe Node.js and npm in the current execution environment using fixed version commands only."""
    fmt = str(args.get("format", "text")).lower()
    if fmt not in {"text", "json"}:
        return 'ERROR: format must be "text" or "json"'
    runner = getattr(getattr(ctx, "core", None), "runner", None)
    if runner is None or not callable(getattr(runner, "run", None)):
        return "ERROR: execution runner is not available"
    agent_dir = getattr(ctx, "agent_dir", None)
    use_agent_cwd = bool(agent_dir is not None and runner_accepts_cwd(runner))
    report: dict[str, Any] = {
        "runner": str(getattr(runner, "kind", "unknown") or "unknown"),
        "agent_home_context": use_agent_cwd,
        "node": {"available": False, "version": None, "status": "not_checked"},
        "npm": {"available": False, "version": None, "status": "not_checked"},
    }
    for key, command in (("node", "node --version"), ("npm", "npm --version")):
        try:
            call_args: dict[str, Any] = {"timeout": 5}
            if use_agent_cwd:
                call_args["cwd"] = agent_dir
            result = await runner.run(command, **call_args)
            if bool(getattr(result, "timed_out", False)):
                report[key]["status"] = "timeout"
                continue
            if int(getattr(result, "exit_code", 1)) == 0:
                # Shell startup hooks can print arbitrary text before the version; keep only
                # a conventional numeric version token rather than returning a banner.
                out = str(getattr(result, "stdout", "") or "").strip().splitlines()
                version_re = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
                versions = [line.strip() for line in out if version_re.fullmatch(line.strip())]
                version = versions[0] if versions else "available (version not parsed)"
                report[key].update(available=True, version=version, status="available")
            else:
                # Don't echo stderr: it can contain paths, shell startup messages or injected text.
                report[key]["status"] = "missing_or_unavailable"
        except Exception as exc:  # noqa: BLE001 - report only the exception class
            report[key]["status"] = "error"
            report[key]["error_type"] = type(exc).__name__
    if fmt == "json":
        return _cap(json.dumps(report, indent=2, sort_keys=True), PREFLIGHT_MAX_CHARS)
    lines = [
        f"node_runtime (bounded probe; runner={report['runner']}; per_agent_home={'yes' if use_agent_cwd else 'no'})",
    ]
    for key in ("node", "npm"):
        item = report[key]
        lines.append(f"{key}: {item['version'] if item['available'] else item['status']}")
    return _cap("\n".join(lines), PREFLIGHT_MAX_CHARS)


# --------------------------------------------------------------------------- env_report

ENV_REPORT_MAX_VARS = 200
ENV_REPORT_MAX_VALUE = 200
ENV_REPORT_MAX_CHARS = 8000
ENV_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL",
                      "AUTH", "PRIVATE", "COOKIE", "SESSION", "SIGNING", "CERT")
_PREFIX_RE = re.compile(r"[A-Z0-9_]{0,64}")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/@\s]*@")
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:[a-z0-9_\-]*(?:token|key|secret|password|passwd|auth|sig|signature|session|code)"
    r"[a-z0-9_\-]*)=)[^&#\s]*")
# Values that look like credentials no matter what the variable is called.
_TOKENISH_RE = re.compile(
    r"(?i)(?:\bbearer\s+\S+|(?:sk|pk|rk|xai|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs])"
    r"[-_][A-Za-z0-9_\-]{12,}|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")


def env_secret_name(name: str) -> bool:
    up = str(name).upper()
    return any(m in up for m in ENV_SECRET_MARKERS)


def redact_value(name: str, value: Any) -> str:
    """How one setting may appear in env_report. Never returns a secret value."""
    if env_secret_name(name):
        return "***set***" if value not in (None, "") else "unset"
    if value is None:
        return "unset"
    v = str(value)
    if _TOKENISH_RE.search(v):
        return "***redacted (token-shaped value)***"
    v = _URL_USERINFO_RE.sub(r"\1***@", v)
    v = _SECRET_QUERY_RE.sub(r"\1***", v)
    v = re.sub(r"[\x00-\x1f\x7f]", "?", v)
    return v[:ENV_REPORT_MAX_VALUE] + ("..." if len(v) > ENV_REPORT_MAX_VALUE else "")


def env_report(ctx: "AgentContext", prefix: str = "") -> dict[str, Any]:
    """Structured, redacted view of the settings in effect for this agent."""
    settings: dict[str, str] = {}
    s = getattr(ctx.core, "settings", None)
    if s is not None and dataclasses.is_dataclass(s):
        for f in dataclasses.fields(s):
            settings[f.name] = redact_value(f.name, getattr(s, f.name, None))
    full = "AVRONE_" + prefix if prefix and not prefix.startswith("AVRONE_") else (prefix or "AVRONE_")
    names = sorted(k for k in os.environ if k.startswith(full))
    env = {k: redact_value(k, os.environ.get(k)) for k in names[:ENV_REPORT_MAX_VARS]}
    if prefix:
        want = full[len("AVRONE_"):].lower()
        settings = {k: v for k, v in settings.items() if k.startswith(want)}
    return {"prefix": full, "settings": settings, "env": env, "env_total": len(names),
            "env_truncated": max(0, len(names) - ENV_REPORT_MAX_VARS),
            "redacted": sum(1 for v in [*settings.values(), *env.values()] if v.startswith("***"))}


async def t_env_report(ctx: "AgentContext", args: dict[str, Any]) -> str:
    prefix = str(args.get("prefix") or "").strip().upper()
    if not _PREFIX_RE.fullmatch(prefix):
        return "ERROR: prefix must be letters, digits or '_' (e.g. \"AVRONE_LLM\" or \"LLM\")"
    rep = env_report(ctx, prefix)
    if str(args.get("format", "text")).lower() == "json":
        return _cap(json.dumps(rep, indent=1, sort_keys=True), ENV_REPORT_MAX_CHARS)
    lines = [f"env_report ({rep['prefix']}*; read-only; secret values never shown, {rep['redacted']} redacted)",
             "settings in effect:"]
    lines += [f"  {k} = {v}" for k, v in rep["settings"].items()] or ["  (none)"]
    lines.append(f"environment ({rep['env_total']} {rep['prefix']}* vars):")
    lines += [f"  {k} = {v}" for k, v in rep["env"].items()] or ["  (none set)"]
    if rep["env_truncated"]:
        lines.append(f"  ...[{rep['env_truncated']} more not shown; narrow with prefix]")
    return _cap("\n".join(lines), ENV_REPORT_MAX_CHARS)


INTROSPECT_TOOLS: list[Tool] = [
    Tool("inspect_tool",
         "read-only: show ONE registered tool's full description, args, arg schema, source and module "
         "(name), or list tools whose name/description contains a substring (query, max 50).",
         '{"name"?: str, "query"?: str}', t_inspect_tool, source="builtin",
         schema={"optional": {"name": "str", "query": "str"}}),
    Tool("env_report",
         "read-only: the AVRONE settings in effect for you plus AVRONE_* env vars, redacted (secret-shaped "
         "names show set/unset only; URL credentials and token-shaped values masked); optional prefix filter.",
         '{"prefix"?: str, "format"?: "text"|"json"}', t_env_report, source="builtin",
         schema={"optional": {"prefix": "str", "format": "str"}}),
    Tool("runtime_preflight",
         "read-only: inspect local Python, runner, workspace access, Docker availability and redacted model/admin readiness; no commands, writes or network probes.",
         '{"format"?: "text"|"json"}', t_runtime_preflight, source="builtin",
         schema={"optional": {"format": "str"}}),
    Tool("node_runtime",
         "bounded: check Node.js and npm versions in the active runner (and your per-agent cwd when available); executes only fixed version probes, never installs packages or starts a server.",
         '{"format"?: "text"|"json"}', t_node_runtime, source="builtin",
         schema={"optional": {"format": "str"}}),
]


def register_introspect_tools(registry) -> None:
    for t in INTROSPECT_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema else None))
