"""Dependency-free MCP (Model Context Protocol) client.

Speaks JSON-RPC 2.0 over two transports:
  * stdio            - spawn a process, newline-delimited JSON on stdin/stdout
  * Streamable HTTP  - POST JSON-RPC to a URL; the reply may be application/json
                       or text/event-stream; the Mcp-Session-Id header is kept.

Servers come from an ``mcp.json`` in Claude Desktop format::

    {"mcpServers": {
        "fs":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."], "env": {}},
        "web":  {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
    }}

Path: env AVRONE_MCP_CONFIG (default ./mcp.json).

Every remote tool is registered into AVRONE's ToolRegistry as
``mcp__<server>__<tool>`` with ``source="mcp:<server>"``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .tools import Tool, ToolRegistry

log = logging.getLogger("avrone.mcp")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "avrone", "version": "0.2.0"}
DEFAULT_TIMEOUT = float(os.getenv("AVRONE_MCP_TIMEOUT", "60"))


class MCPError(RuntimeError):
    """A JSON-RPC error or transport failure talking to an MCP server."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _safe_part(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", s) or "_"


# --------------------------------------------------------------------------- transports
class _Transport:
    async def connect(self) -> None: ...
    async def request(self, method: str, params: dict | None, timeout: float) -> Any: ...
    async def notify(self, method: str, params: dict | None = None) -> None: ...
    async def close(self) -> None: ...


class StdioTransport(_Transport):
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None):
        self.command = command
        self.args = [str(a) for a in (args or [])]
        self.env = {k: str(v) for k, v in (env or {}).items()}
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self.stderr_tail: list[str] = []

    async def connect(self) -> None:
        env = {**os.environ, **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.cwd, limit=16 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        with contextlib.suppress(Exception):
            async for raw in self.proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self.stderr_tail = (self.stderr_tail + [line])[-20:]
                    log.debug("mcp stderr [%s]: %s", self.command, line)

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("mcp non-JSON stdout line ignored: %s", line[:200])
                    continue
                for m in (msg if isinstance(msg, list) else [msg]):
                    await self._dispatch(m)
        except Exception as e:  # pragma: no cover
            log.debug("mcp stdio reader stopped: %s", e)
        finally:
            err = MCPError("MCP server process exited" + (
                f": {self.stderr_tail[-1]}" if self.stderr_tail else ""))
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _dispatch(self, m: Any) -> None:
        if not isinstance(m, dict):
            return
        if "id" in m and ("result" in m or "error" in m):
            fut = self._pending.pop(m["id"], None)
            if fut and not fut.done():
                fut.set_result(m)
        elif "method" in m and "id" in m:
            # Server->client request (ping, sampling, roots...). Answer ping, decline the rest.
            if m["method"] == "ping":
                await self._send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
            else:
                await self._send({"jsonrpc": "2.0", "id": m["id"],
                                  "error": {"code": -32601, "message": "Method not supported by client"}})
        # notifications (logging, progress, list_changed) are ignored

    async def _send(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise MCPError("MCP server process is not running")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None, timeout: float) -> Any:
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"timeout waiting for {method}")
        finally:
            self._pending.pop(rid, None)
        return _unwrap(resp)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def close(self) -> None:
        p = self.proc
        if p and p.returncode is None:
            with contextlib.suppress(Exception):
                p.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(p.wait(), 2)
            except (asyncio.TimeoutError, Exception):
                with contextlib.suppress(ProcessLookupError):
                    p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 2)
                except (asyncio.TimeoutError, Exception):
                    with contextlib.suppress(ProcessLookupError):
                        p.kill()
        for t in (self._reader, self._stderr_task):
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t


class HTTPTransport(_Transport):
    """MCP Streamable HTTP transport."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = {k: str(v) for k, v in (headers or {}).items()}
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._id = 0
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=15),
                                         follow_redirects=True)

    def _hdrs(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def _post(self, body: dict, timeout: float, want_id: int | None) -> Any:
        assert self._client is not None, "connect() first"
        async with self._client.stream("POST", self.url, json=body, headers=self._hdrs(),
                                       timeout=timeout) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            if r.status_code == 404 and self.session_id and body.get("method") != "initialize":
                raise MCPError("MCP session expired (HTTP 404)", code=404)
            if r.status_code >= 400:
                text = (await r.aread()).decode(errors="replace")
                raise MCPError(f"HTTP {r.status_code}: {text[:300]}", code=r.status_code)
            if want_id is None:  # notification: 202 Accepted, nothing to read
                return None
            ctype = r.headers.get("content-type", "").lower()
            if "text/event-stream" in ctype:
                data_lines: list[str] = []
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "" and data_lines:
                        payload, data_lines = "\n".join(data_lines), []
                        found = _match_response(payload, want_id)
                        if found is not None:
                            return found
                if data_lines:
                    found = _match_response("\n".join(data_lines), want_id)
                    if found is not None:
                        return found
                raise MCPError("event stream ended without a response")
            raw = await r.aread()
            if not raw.strip():
                raise MCPError("empty response body")
            found = _match_response(raw.decode(errors="replace"), want_id)
            if found is None:
                raise MCPError("no matching JSON-RPC response in body")
            return found

    async def request(self, method: str, params: dict | None, timeout: float) -> Any:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            resp = await self._post(msg, timeout, self._id)
        except httpx.HTTPError as e:
            raise MCPError(f"{type(e).__name__}: {e}")
        return _unwrap(resp)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._post(msg, DEFAULT_TIMEOUT, None)
        except (httpx.HTTPError, MCPError) as e:
            log.debug("mcp notify %s failed: %s", method, e)

    async def close(self) -> None:
        if self._client is None:
            return
        if self.session_id:  # politely end the session; servers may answer 405
            with contextlib.suppress(Exception):
                await self._client.delete(self.url, headers=self._hdrs(), timeout=5)
        await self._client.aclose()
        self._client = None


def _match_response(payload: str, want_id: int) -> dict | None:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    for m in (obj if isinstance(obj, list) else [obj]):
        if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
            return m
    return None


def _unwrap(resp: dict) -> Any:
    if "error" in resp and resp["error"] is not None:
        err = resp["error"] if isinstance(resp["error"], dict) else {"message": str(resp["error"])}
        raise MCPError(str(err.get("message", "error")), err.get("code"), err.get("data"))
    return resp.get("result")


# --------------------------------------------------------------------------- client
class MCPClient:
    """One connection to one MCP server."""

    def __init__(self, name: str, config: dict[str, Any], timeout: float = DEFAULT_TIMEOUT):
        self.name = name
        self.config = config
        self.timeout = float(config.get("timeout", timeout))
        if config.get("url"):
            self.transport: _Transport = HTTPTransport(str(config["url"]), config.get("headers"))
            self.kind = "http"
        elif config.get("command"):
            self.transport = StdioTransport(str(config["command"]), config.get("args"),
                                            config.get("env"), config.get("cwd"))
            self.kind = "stdio"
        else:
            raise MCPError(f"server '{name}' needs either 'command' or 'url'")
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.tools: list[dict[str, Any]] = []

    async def connect(self) -> None:
        await self.transport.connect()
        await self.initialize()

    async def initialize(self) -> dict[str, Any]:
        res = await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.timeout) or {}
        version = res.get("protocolVersion") or PROTOCOL_VERSION
        if version not in SUPPORTED_VERSIONS:
            log.warning("MCP server %s negotiated unknown protocol %s; continuing", self.name, version)
        self.protocol_version = version
        if isinstance(self.transport, HTTPTransport):
            self.transport.protocol_version = version
        self.server_info = res.get("serverInfo") or {}
        self.capabilities = res.get("capabilities") or {}
        await self.transport.notify("notifications/initialized")
        return res

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):  # pagination guard
            params = {"cursor": cursor} if cursor else {}
            res = await self.transport.request("tools/list", params, timeout=self.timeout) or {}
            tools.extend(t for t in res.get("tools", []) if isinstance(t, dict) and t.get("name"))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        res = await self.transport.request("tools/call", {"name": tool, "arguments": arguments or {}},
                                           timeout=self.timeout) or {}
        text = format_content(res)
        if res.get("isError"):
            return f"ERROR (from MCP tool {self.name}/{tool}): {text}"
        return text

    async def ping(self) -> bool:
        await self.transport.request("ping", None, timeout=10)
        return True

    async def close(self) -> None:
        await self.transport.close()


def format_content(result: dict[str, Any]) -> str:
    """Flatten an MCP CallToolResult into text for an agent."""
    parts: list[str] = []
    for c in result.get("content") or []:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        kind = c.get("type")
        if kind == "text":
            parts.append(str(c.get("text", "")))
        elif kind == "image":
            parts.append(f"[image: {c.get('mimeType', 'unknown')}, {len(str(c.get('data', '')))} b64 chars omitted]")
        elif kind == "audio":
            parts.append(f"[audio: {c.get('mimeType', 'unknown')} omitted]")
        elif kind == "resource":
            r = c.get("resource") or {}
            if "text" in r:
                parts.append(f"[resource {r.get('uri', '')}]\n{r['text']}")
            else:
                parts.append(f"[resource: {r.get('uri', '')} ({r.get('mimeType', 'binary')}) omitted]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {c.get('uri', '')} {c.get('name', '')}]".strip())
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts) if parts else "(no content)"


_TYPE_MAP = {"string": "str", "integer": "int", "number": "float", "boolean": "bool",
             "array": "list", "object": "dict", "null": "null"}


def args_doc_from_schema(schema: dict[str, Any] | None, limit: int = 300) -> str:
    """Compact '{"path": str, "depth"?: int}' style doc from a JSON Schema."""
    if not isinstance(schema, dict):
        return "{}"
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    items = []
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        t = spec.get("type")
        if isinstance(t, list):
            t = "|".join(_TYPE_MAP.get(x, str(x)) for x in t if x != "null") or "any"
        elif "enum" in spec:
            t = "|".join(json.dumps(v) for v in spec["enum"][:6])
        else:
            t = _TYPE_MAP.get(t, "any") if t else "any"
        items.append(f'"{key}"{"" if key in required else "?"}: {t}')
    doc = "{" + ", ".join(items) + "}"
    if len(doc) > limit:
        doc = doc[: limit - 5].rsplit(",", 1)[0] + ", ...}"
    return doc


def load_config(path: str | os.PathLike | None = None) -> dict[str, dict[str, Any]]:
    """Read the mcpServers map. Missing file -> {}."""
    p = Path(path or os.getenv("AVRONE_MCP_CONFIG", "./mcp.json"))
    if not p.exists():
        return {}
    data = json.loads(p.read_text() or "{}")
    servers = data.get("mcpServers", data.get("servers", {})) if isinstance(data, dict) else {}
    return {str(k): v for k, v in servers.items()
            if isinstance(v, dict) and not v.get("disabled")}


# --------------------------------------------------------------------------- manager
class MCPManager:
    """Connects every server in mcp.json and exposes their tools to AVRONE agents."""

    def __init__(self, config_path: str | os.PathLike | None = None,
                 servers: dict[str, dict[str, Any]] | None = None):
        self.config_path = config_path
        self._servers_override = servers
        self.clients: dict[str, MCPClient] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._registered: list[str] = []
        self.registry: ToolRegistry | None = None

    async def start(self, registry: ToolRegistry) -> dict[str, dict[str, Any]]:
        self.registry = registry
        try:
            servers = self._servers_override if self._servers_override is not None \
                else load_config(self.config_path)
        except Exception as e:
            log.error("could not read MCP config: %s", e)
            self._status["_config"] = {"connected": False, "error": f"config: {e}", "tools": 0}
            return self.status()
        await asyncio.gather(*(self._start_one(n, c, registry) for n, c in servers.items()))
        return self.status()

    async def _start_one(self, name: str, cfg: dict[str, Any], registry: ToolRegistry) -> None:
        client: MCPClient | None = None
        try:
            client = MCPClient(name, cfg)
            await asyncio.wait_for(client.connect(), client.timeout)
            tools = await client.list_tools()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}" or type(e).__name__
            log.warning("MCP server '%s' failed to start: %s", name, msg)
            self._status[name] = {"connected": False, "error": msg, "tools": 0,
                                  "transport": client.kind if client else None}
            if client:
                with contextlib.suppress(Exception):
                    await client.close()
            return
        self.clients[name] = client
        count = 0
        for t in tools:
            tool_name = f"mcp__{_safe_part(name)}__{_safe_part(t['name'])}"
            try:
                registry.register(Tool(
                    name=tool_name,
                    description=(t.get("description") or t.get("title") or f"MCP tool {t['name']} on {name}").strip().replace("\n", " ")[:500],
                    args_doc=args_doc_from_schema(t.get("inputSchema")),
                    fn=self._make_fn(client, t["name"]),
                    source=f"mcp:{name}",
                ))
                self._registered.append(tool_name)
                count += 1
            except ValueError as e:
                log.warning("skipping MCP tool %s/%s: %s", name, t.get("name"), e)
        self._status[name] = {"connected": True, "error": None, "tools": count, "transport": client.kind,
                              "server": client.server_info, "protocol": client.protocol_version}
        log.info("MCP server '%s' connected with %d tools", name, count)

    @staticmethod
    def _make_fn(client: MCPClient, remote: str):
        async def fn(ctx: Any, args: dict[str, Any]) -> str:
            try:
                return await client.call_tool(remote, args if isinstance(args, dict) else {})
            except MCPError as e:
                return f"ERROR: MCP {client.name}/{remote}: {e}"
        return fn

    async def stop(self) -> None:
        if self.registry is not None:
            for n in self._registered:
                self.registry.unregister(n)
        self._registered.clear()
        await asyncio.gather(*(c.close() for c in self.clients.values()), return_exceptions=True)
        for name in self.clients:
            if name in self._status:
                self._status[name]["connected"] = False
        self.clients.clear()

    def status(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._status.items()}
