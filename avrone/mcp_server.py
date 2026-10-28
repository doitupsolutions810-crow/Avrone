"""Expose AVRONE itself as an MCP server over stdio.

    python -m avrone.mcp_server

Add to any MCP client (Claude Desktop, Cursor, another AVRONE...)::

    {"mcpServers": {"avrone": {"command": "python", "args": ["-m", "avrone.mcp_server"],
                               "env": {"AVRONE_LLM_BASE_URL": "...", "AVRONE_WORKSPACE": "..."}}}}

Tools: avrone_squad, avrone_chat, avrone_deep, avrone_tools.
stdout carries JSON-RPC only; all logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger("avrone.mcp_server")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
from . import __version__ as _AVRONE_VERSION  # noqa: E402

SERVER_INFO = {"name": "avrone", "title": "AVRONE multi-agent squad", "version": _AVRONE_VERSION}
_CHILD_ENV = "AVRONE_MCP_SERVER_CHILD"  # stops an AVRONE MCP server from loading itself recursively

DEEP_PROMPT = """Deep analysis requested. Work the task below thoroughly: research it, test claims
with tools where possible, look for errors and missing angles, and argue the strongest objections.
{prior}
TASK:
{task}"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "avrone_squad",
        "title": "AVRONE squad (Grok Heavy style)",
        "description": "Run a task with a parallel squad of AVRONE agents (planner splits it into angles, "
                       "agents work them with real tools, a lead cross-checks and merges one answer).",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "What the squad should do or answer."},
            "squad_size": {"type": "integer", "minimum": 1, "maximum": 8,
                           "description": "Number of parallel agents (default from AVRONE_SQUAD_SIZE)."},
            "checkpoint_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$",
                              "description": "Server-managed checkpoint; call again with the same id and "
                                             "task to resume an interrupted run."},
        }, "required": ["task"]},
    },
    {
        "name": "avrone_chat",
        "title": "AVRONE single agent",
        "description": "Run a quick task with one tool-using AVRONE agent.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "The request."},
        }, "required": ["task"]},
    },
    {
        "name": "avrone_deep",
        "title": "AVRONE deep analysis",
        "description": "Deep, adversarial analysis of a task; optionally critiques and improves a first answer.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "The question or task."},
            "first_answer": {"type": "string", "description": "Optional draft answer to verify and improve."},
            "rounds": {"type": "integer", "minimum": 1, "maximum": 5,
                       "description": "Max deep waves (1-5, default AVRONE_DEEP_ROUNDS or 1); "
                                      "stops early once a wave finds nothing new."},
            "checkpoint_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$",
                              "description": "Server-managed checkpoint; call again with the same id and "
                                             "task to resume an interrupted run."},
        }, "required": ["task"]},
    },
    {
        "name": "avrone_tools",
        "title": "List AVRONE tools",
        "description": "List the tools AVRONE agents can use (built-ins, plugins, MCP).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def build_core():
    """Build a TrinityCore the same way the web server does (plus plugins and MCP tools)."""
    from .codex import Codex
    from .config import settings
    from .llm import LLM
    from .sovereign import establish
    from .trinity import TrinityCore

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    runner = await establish(settings)
    core = TrinityCore(settings, LLM(settings), runner, Codex(settings.data_dir))
    extras: dict[str, Any] = {"runner": runner, "mcp": None}
    try:
        from .plugins import load_plugins
        load_plugins(core.tools, settings)
    except Exception as e:  # never fatal
        log.warning("plugins not loaded: %s", e)
    if not os.getenv(_CHILD_ENV):
        os.environ[_CHILD_ENV] = "1"  # inherited by MCP children, so a nested AVRONE skips this step
        try:
            from .mcp_client import MCPManager
            mgr = MCPManager()
            await mgr.start(core.tools)
            extras["mcp"] = mgr
        except Exception as e:
            log.warning("MCP servers not loaded: %s", e)
    return core, extras


class AvroneMCPServer:
    def __init__(self, core_factory=build_core):
        self._factory = core_factory
        self._core_task: asyncio.Task | None = None
        self._extras: dict[str, Any] = {}
        self._out = None
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- core
    async def core(self):
        if self._core_task is None:
            self._core_task = asyncio.create_task(self._factory())
        core, self._extras = await self._core_task
        return core

    def warm(self) -> None:
        if self._core_task is None:
            self._core_task = asyncio.create_task(self._factory())

    # ---------------------------------------------------------------- io
    async def send(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            self._out.write(data)
            self._out.flush()

    async def serve(self, reader: asyncio.StreamReader, out) -> None:
        self._out = out
        self.warm()
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                await self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                t = asyncio.create_task(self._handle_safe(m))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.shutdown()

    async def shutdown(self) -> None:
        if self._core_task and self._core_task.done() and not self._core_task.exception():
            mgr = self._extras.get("mcp")
            if mgr:
                with contextlib.suppress(Exception):
                    await mgr.stop()
            runner = self._extras.get("runner")
            if runner:
                with contextlib.suppress(Exception):
                    await runner.stop()

    async def _handle_safe(self, m: Any) -> None:
        if not isinstance(m, dict) or "method" not in m:
            return  # responses to requests we never send, or junk
        rid = m.get("id")
        is_request = "id" in m
        try:
            result = await self.handle(m["method"], m.get("params") or {})
            if is_request:
                await self.send({"jsonrpc": "2.0", "id": rid, "result": result})
        except _RPCError as e:
            if is_request:
                await self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}})
        except Exception as e:
            log.exception("handler failed")
            if is_request:
                await self.send({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})

    # ---------------------------------------------------------------- methods
    async def handle(self, method: str, params: dict) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            return {"protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "AVRONE runs tasks with a parallel squad of tool-using agents. "
                                    "Use avrone_squad for substantive work, avrone_chat for quick asks."}
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return await self.call_tool(str(params.get("name", "")), params.get("arguments") or {})
        raise _RPCError(-32601, f"Method not found: {method}")

    async def call_tool(self, name: str, args: dict) -> dict:
        if name not in {t["name"] for t in TOOLS}:
            raise _RPCError(-32602, f"Unknown tool: {name}")
        task = str(args.get("task", "")).strip()
        try:
            core = await self.core()
            if name == "avrone_tools":
                return _ok(json.dumps(core.tools.list(), indent=2), {"tools": core.tools.list()})
            if not task:
                return _err("'task' is required")
            ck, release = _checkpoint(core, name, task, args.get("checkpoint_id"))
            try:
                res = await self._run(core, name, task, args, ck)
            finally:
                if release:
                    release()
        except Exception as e:
            log.exception("tool %s failed", name)
            return _err(f"{type(e).__name__}: {e}")
        if isinstance(res, dict):
            answer = str(res.get("answer", ""))
            meta = {"run_id": res.get("run_id"), "agents": len(res.get("agents") or [])}
            if ck:
                meta["checkpoint_id"] = str(args.get("checkpoint_id"))
            return _ok(answer, meta)
        return _ok(str(res))

    async def _run(self, core: Any, name: str, task: str, args: dict, ck: dict) -> Any:
        if name == "avrone_squad":
            res = await core.squad(task, _clamp_size(args.get("squad_size")), **ck)
        elif name == "avrone_chat":
            res = await core.chat(task)
        else:  # avrone_deep
            first = args.get("first_answer")
            if hasattr(core, "deep"):
                rounds = _clamp_rounds(args.get("rounds"))
                kw = {"rounds": rounds} if rounds is not None else {}
                res = await core.deep(task, first, **kw, **ck)
            else:
                prior = (f"A first answer was given; verify it, fix its mistakes and improve it:\n{first}\n"
                         if first else "")
                res = await core.squad(DEEP_PROMPT.format(prior=prior, task=task))
        return res


def _checkpoint(core: Any, name: str, task: str, ck_id: Any) -> tuple[dict, Any]:
    """({"checkpoint": path} or {}, release or None). Raises CheckpointError/CheckpointConflict."""
    if ck_id in (None, "") or name not in ("avrone_squad", "avrone_deep"):
        return {}, None
    from .checkpoint import check_compatible, claim, managed_path, sanitize_client_id
    path = managed_path(core.settings.data_dir, sanitize_client_id(ck_id))
    check_compatible(path, task, "squad" if name == "avrone_squad" else "deep")
    return {"checkpoint": path}, claim(path)


def _clamp_size(size: Any) -> int | None:
    """Honour the advertised schema (1..8); junk or missing means 'use the default'."""
    if size is None or isinstance(size, bool):
        return None
    try:
        n = int(size)
    except (TypeError, ValueError):
        return None
    return max(1, min(8, n))


def _clamp_rounds(rounds: Any) -> int | None:
    """Honour the advertised schema (1..5); junk or missing means 'use the default'."""
    from .trinity import clamp_deep_rounds
    return clamp_deep_rounds(rounds)


class _RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _ok(text: str, structured: dict | None = None) -> dict:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


async def amain() -> None:
    out = sys.stdout.buffer
    # Anything else that prints must not corrupt the JSON-RPC stream.
    sys.stdout = sys.stderr
    server = AvroneMCPServer()
    await server.serve(await _stdin_reader(), out)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=os.getenv("AVRONE_LOG_LEVEL", "WARNING").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
