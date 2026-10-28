"""Bootstrap: build a ready TrinityCore in one place, for the server and the CLI.

Order: establish the environment (runner) first, then Codex, LLM, the tool
registry and the core, then optional extensions if their modules exist:

    avrone.tools_extra.register_extra_tools  -> extra built-in tools
    avrone.plugins.load_plugins              -> user plugins
    avrone.mcp_client.MCPManager             -> tools from MCP servers

Each hook is optional (ImportError = not installed) and isolated: a hook that
raises is recorded in core.extensions["errors"] and the core still starts.
Hooks are called with whichever of (registry, settings, core) their
signature asks for, by name (registry/tools, settings/config, core) or, for
unnamed required parameters, in that order.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from .codex import Codex
from .config import Settings
from .llm import LLM
from .sovereign import establish
from .tools import ToolRegistry, builtin_registry
from .trinity import TrinityCore

log = logging.getLogger("avrone.bootstrap")

Cleanup = Callable[[], Any]

_ALIASES = {
    "registry": "registry", "reg": "registry", "tools": "registry", "tool_registry": "registry",
    "settings": "settings", "config": "settings", "s": "settings", "cfg": "settings",
    "core": "core", "trinity": "core",
}


def _flex_args(fn: Callable, env: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Work out how to call fn with the objects in env, from its signature."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return [env["registry"]], {}
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    order = iter([env["registry"], env["settings"], env["core"]])
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        key = _ALIASES.get(p.name)
        if key is not None:
            if p.kind == p.POSITIONAL_ONLY:
                args.append(env[key])
            else:
                kwargs[p.name] = env[key]
        elif p.default is p.empty:
            val = next(order, None)
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                args.append(val)
            else:
                kwargs[p.name] = val
    return args, kwargs


async def _call(fn: Callable, env: dict[str, Any]) -> Any:
    args, kwargs = _flex_args(fn, env)
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


def _describe(obj: Any) -> Any:
    """Turn a hook's return value into something JSON-friendly for /api/extensions."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _describe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_describe(v) for v in obj]
    for attr in ("status", "to_dict", "as_dict"):
        m = getattr(obj, attr, None)
        if callable(m):
            try:
                return _describe(m())
            except Exception:
                pass
    name = getattr(obj, "name", None)
    return str(name) if name else repr(obj)[:200]


def _sources(registry: ToolRegistry) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in registry.list():
        counts[t["source"]] = counts.get(t["source"], 0) + 1
    return counts


async def build_core(settings: Settings, *, extras: bool = True) -> tuple[TrinityCore, list[Cleanup]]:
    """Establish the environment and build the core. Returns (core, cleanups).

    Run the cleanups (sync or async callables) on shutdown with run_cleanups().
    core.extensions holds what the optional hooks loaded, for /api/extensions.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    runner = await establish(settings)  # environment FIRST
    cleanups: list[Cleanup] = [runner.stop]
    codex = Codex(settings.data_dir)
    registry = builtin_registry()
    core = TrinityCore(settings, LLM(settings), runner, codex, tools=registry)
    ext: dict[str, Any] = {"extra_tools": None, "plugins": None, "mcp": None, "errors": []}
    core.extensions = ext  # type: ignore[attr-defined]
    if not extras:
        ext["tool_sources"] = _sources(registry)
        return core, cleanups
    env = {"registry": registry, "settings": settings, "core": core}

    # 1. extra built-in tools
    try:
        from .tools_extra import register_extra_tools  # type: ignore[import-not-found]
    except ImportError:
        ext["extra_tools"] = {"installed": False}
    else:
        before = set(registry.names())
        try:
            res = await _call(register_extra_tools, env)
            ext["extra_tools"] = {"installed": True, "added": sorted(set(registry.names()) - before),
                                  "result": _describe(res)}
        except Exception as e:
            ext["extra_tools"] = {"installed": True, "error": f"{type(e).__name__}: {e}"}
            ext["errors"].append(f"tools_extra: {type(e).__name__}: {e}")
            log.exception("register_extra_tools failed")

    # 2. plugins
    try:
        from .plugins import load_plugins  # type: ignore[import-not-found]
    except ImportError:
        ext["plugins"] = {"installed": False}
    else:
        before = set(registry.names())
        try:
            res = await _call(load_plugins, env)
            ext["plugins"] = {"installed": True, "loaded": _describe(res),
                              "added": sorted(set(registry.names()) - before)}
        except Exception as e:
            ext["plugins"] = {"installed": True, "error": f"{type(e).__name__}: {e}"}
            ext["errors"].append(f"plugins: {type(e).__name__}: {e}")
            log.exception("load_plugins failed")

    # 3. MCP servers
    try:
        from .mcp_client import MCPManager  # type: ignore[import-not-found]
    except ImportError:
        ext["mcp"] = {"installed": False}
    else:
        before = set(registry.names())
        mgr = None
        try:
            mgr = await _call(MCPManager, env)
            core.mcp = mgr  # type: ignore[attr-defined]
            for meth in ("start", "connect_all", "load", "connect"):
                fn = getattr(mgr, meth, None)
                if callable(fn):
                    await _call(fn, env)
                    break
            reg_fn = getattr(mgr, "register_tools", None) or getattr(mgr, "register_all", None)
            if callable(reg_fn):
                await _call(reg_fn, env)
            for meth in ("stop", "aclose", "close", "shutdown"):
                fn = getattr(mgr, meth, None)
                if callable(fn):
                    cleanups.append(fn)
                    break
            status = getattr(mgr, "status", None)
            status = status() if callable(status) else (status or getattr(mgr, "servers", None))
            if inspect.isawaitable(status):
                status = await status
            ext["mcp"] = {"installed": True, "servers": _describe(status),
                          "added": sorted(set(registry.names()) - before)}
        except Exception as e:
            ext["mcp"] = {"installed": True, "error": f"{type(e).__name__}: {e}"}
            ext["errors"].append(f"mcp: {type(e).__name__}: {e}")
            log.exception("MCPManager failed")

    # 4. fleet: link to other AVRONE nodes (needs AVRONE_FLEET_TOKEN)
    try:
        from .fleet import FleetManager, register_fleet_tools
        fleet = FleetManager(core, settings.data_dir)
        core.fleet = fleet  # type: ignore[attr-defined]
        if fleet.enabled:
            register_fleet_tools(registry)
            fleet.start_queue(settings.data_dir)
            fleet.start()
            cleanups.append(fleet.stop)
        ext["fleet"] = fleet.info()
    except Exception as e:
        ext["fleet"] = {"error": f"{type(e).__name__}: {e}"}
        ext["errors"].append(f"fleet: {type(e).__name__}: {e}")
        log.exception("fleet failed")

    ext["tool_sources"] = _sources(registry)
    return core, cleanups


async def run_cleanups(cleanups: list[Cleanup]) -> None:
    """Run cleanups in reverse order; one failing does not stop the rest."""
    for fn in reversed(cleanups):
        try:
            res = fn()
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("cleanup failed")
