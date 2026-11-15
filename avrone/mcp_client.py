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
import hashlib
import json
import logging
import math
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
MAX_TOOL_PAGES = 100
MAX_REGISTERED_TOOLS = 2048
MAX_MCP_SERVERS = 64
MAX_MCP_CONFIG_BYTES = 1024 * 1024
MAX_PAGE_CURSOR_CHARS = 512
MAX_TOOL_OUTPUT_CHARS = 32_000
MAX_TOOL_OUTPUT_ITEMS = 512
MAX_SCHEMA_PROPERTIES = 128
# Bound untrusted MCP HTTP responses before JSON parsing or tool-result formatting.
MAX_HTTP_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_HTTP_SSE_LINE_BYTES = 1024 * 1024
MAX_HTTP_ERROR_BYTES = 64 * 1024
MAX_STDIO_LINE_BYTES = 8 * 1024 * 1024
MAX_RPC_REQUEST_BYTES = 8 * 1024 * 1024


class MCPError(RuntimeError):
    """A JSON-RPC error or transport failure talking to an MCP server."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects rather than silently keeping the last duplicate key."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _strict_json_loads(payload: str) -> Any:
    """Parse JSON while rejecting duplicate keys, non-standard constants, and deep nesting."""
    try:
        return json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds parser limit") from exc


def _encode_rpc_request(message: dict[str, Any]) -> bytes:
    """Serialize outbound JSON-RPC with strict JSON semantics and a byte ceiling."""
    if not isinstance(message, dict):
        raise MCPError("MCP JSON-RPC request must be a JSON object")
    try:
        encoded = json.dumps(
            message, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise MCPError("MCP JSON-RPC request contains values that are not valid JSON") from exc
    if len(encoded) > MAX_RPC_REQUEST_BYTES:
        raise MCPError(
            f"MCP JSON-RPC request exceeded configured limit ({MAX_RPC_REQUEST_BYTES} bytes)"
        )
    return encoded


def _safe_part(s: str) -> str:
    raw = str(s)
    value = re.sub(r"[^A-Za-z0-9_\-.]", "_", raw)
    if value == raw and len(raw) <= 80:
        return value or "_"
    # Preserve readable names while preventing distinct remote names from colliding
    # after sanitization or truncation in the shared ToolRegistry namespace.
    suffix = hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"{value[:67] or '_'}-{suffix}"


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
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.cwd, limit=MAX_STDIO_LINE_BYTES)
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
                    msg = _strict_json_loads(line)
                except ValueError:
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
        if not isinstance(m, dict) or m.get("jsonrpc") != "2.0":
            return
        if "id" in m and ("result" in m or "error" in m):
            rid = m["id"]
            if type(rid) is not int or not _valid_rpc_response(m, rid):
                return
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(m)
        elif "method" in m and "id" in m:
            # Server->client request (ping, sampling, roots...). Answer ping, decline the rest.
            rid, method = m["id"], m["method"]
            if not _valid_rpc_request_id(rid) or not isinstance(method, str) or not method.strip():
                return
            if method == "ping":
                await self._send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                await self._send({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601, "message": "Method not supported by client"}})
        # notifications (logging, progress, list_changed) are ignored

    async def _send(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise MCPError("MCP server process is not running")
        data = _encode_rpc_request(obj) + b"\n"
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
        # MCP endpoints may carry custom bearer/API-key headers. Do not automatically forward
        # them through redirects to a different origin.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=15),
                                         follow_redirects=False)

    def _hdrs(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def _read_http_body(self, response: Any, limit: int, *, truncate: bool = False) -> bytes:
        """Read a bounded HTTP body without buffering an untrusted response in full."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            remaining = limit - size
            if len(chunk) > remaining:
                if not truncate:
                    raise MCPError(f"HTTP response body exceeded configured limit ({limit} bytes)")
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    async def _read_sse_response(self, response: Any, want_id: int) -> dict:
        """Parse SSE incrementally, bounding both total bytes and any single line."""
        data_lines: list[str] = []
        pending = bytearray()
        total = 0

        def consume_line(raw: bytes) -> dict | None:
            if len(raw) > MAX_HTTP_SSE_LINE_BYTES:
                raise MCPError(
                    f"MCP SSE line exceeded configured limit ({MAX_HTTP_SSE_LINE_BYTES} bytes)"
                )
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            line = raw.decode(errors="replace")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "" and data_lines:
                payload = "\n".join(data_lines)
                data_lines.clear()
                return _match_response(payload, want_id)
            return None

        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_HTTP_RESPONSE_BYTES:
                raise MCPError(
                    f"MCP SSE response exceeded configured limit ({MAX_HTTP_RESPONSE_BYTES} bytes)"
                )
            pending.extend(chunk)
            line_start = 0
            while True:
                newline = pending.find(b"\n", line_start)
                if newline < 0:
                    break
                raw = bytes(pending[line_start:newline])
                line_start = newline + 1
                found = consume_line(raw)
                if found is not None:
                    return found
            if line_start:
                del pending[:line_start]
            if len(pending) > MAX_HTTP_SSE_LINE_BYTES:
                raise MCPError(
                    f"MCP SSE line exceeded configured limit ({MAX_HTTP_SSE_LINE_BYTES} bytes)"
                )

        if pending:
            found = consume_line(bytes(pending))
            if found is not None:
                return found
        if data_lines:
            found = _match_response("\n".join(data_lines), want_id)
            if found is not None:
                return found
        raise MCPError("event stream ended without a response")

    async def _post(self, body: dict, timeout: float, want_id: int | None) -> Any:
        assert self._client is not None, "connect() first"
        encoded_body = _encode_rpc_request(body)
        async with self._client.stream("POST", self.url, content=encoded_body, headers=self._hdrs(),
                                       timeout=timeout) as r:
            if 300 <= r.status_code < 400:
                raise MCPError("HTTP redirect refused for MCP endpoint", code=r.status_code)
            sid = r.headers.get("mcp-session-id")
            if sid:
                method = body.get("method")
                if self.session_id and sid != self.session_id and method != "initialize":
                    raise MCPError("MCP server changed the active session identifier unexpectedly")
                self.session_id = sid
            if r.status_code == 404 and self.session_id and body.get("method") != "initialize":
                raise MCPError("MCP session expired (HTTP 404)", code=404)
            if r.status_code >= 400:
                raw_error = await self._read_http_body(r, MAX_HTTP_ERROR_BYTES, truncate=True)
                text = raw_error.decode(errors="replace")
                raise MCPError(f"HTTP {r.status_code}: {text[:300]}", code=r.status_code)
            if want_id is None:  # notification: 202 Accepted, nothing to read
                return None
            ctype = r.headers.get("content-type", "").lower()
            if "text/event-stream" in ctype:
                return await self._read_sse_response(r, want_id)
            raw = await self._read_http_body(r, MAX_HTTP_RESPONSE_BYTES)
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


def _reject_json_constant(value: str) -> None:
    """Reject Python's non-standard NaN/Infinity JSON extensions."""
    raise ValueError(f"non-standard JSON constant {value!r}")


def _valid_rpc_request_id(value: Any) -> bool:
    """Reject booleans and oversized strings masquerading as JSON-RPC request IDs."""
    return type(value) is int or (isinstance(value, str) and len(value) <= 512)


def _valid_rpc_response(message: Any, want_id: int) -> bool:
    """Validate the JSON-RPC 2.0 response envelope before matching a pending request."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return False
    if type(message.get("id")) is not int or message["id"] != want_id:
        return False
    has_result, has_error = "result" in message, "error" in message
    if has_result == has_error:
        return False
    if has_error:
        error = message["error"]
        return (
            isinstance(error, dict)
            and type(error.get("code")) is int
            and isinstance(error.get("message"), str)
        )
    return True


def _match_response(payload: str, want_id: int) -> dict | None:
    try:
        obj = _strict_json_loads(payload)
    except (TypeError, ValueError):
        return None
    for message in (obj if isinstance(obj, list) else [obj]):
        if _valid_rpc_response(message, want_id):
            return message
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
        raw_timeout = config.get("timeout", timeout)
        try:
            parsed_timeout = float(raw_timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MCPError("MCP timeout must be a finite positive number") from exc
        if isinstance(raw_timeout, bool) or not math.isfinite(parsed_timeout) or parsed_timeout <= 0:
            raise MCPError("MCP timeout must be a finite positive number")
        self.timeout = parsed_timeout
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
        try:
            await self.initialize()
        except BaseException:
            # A failed or cancelled handshake must not leak an HTTP client or stdio server process.
            try:
                await asyncio.shield(self.transport.close())
            except BaseException:
                pass
            raise

    async def initialize(self) -> dict[str, Any]:
        # Reset negotiated state before every handshake. A failed renegotiation must not
        # leave a stale supported version or HTTP header behind.
        self.protocol_version = None
        self.server_info = {}
        self.capabilities = {}
        if isinstance(self.transport, HTTPTransport):
            self.transport.protocol_version = None
        res = await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.timeout)
        if not isinstance(res, dict):
            raise MCPError("MCP initialize response must be a JSON object")
        version = res.get("protocolVersion", PROTOCOL_VERSION)
        if not isinstance(version, str) or not version.strip():
            raise MCPError("MCP initialize protocolVersion must be a non-empty string")
        if version not in SUPPORTED_VERSIONS:
            supported = ", ".join(SUPPORTED_VERSIONS)
            raise MCPError(
                f"MCP server {self.name} negotiated unsupported protocol {version!r}; "
                f"this client supports only: {supported}"
            )
        self.protocol_version = version
        if isinstance(self.transport, HTTPTransport):
            self.transport.protocol_version = version
        server_info = res.get("serverInfo")
        capabilities = res.get("capabilities")
        self.server_info = server_info if isinstance(server_info, dict) else {}
        self.capabilities = capabilities if isinstance(capabilities, dict) else {}
        await self.transport.notify("notifications/initialized")
        return res

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            res = await self.transport.request("tools/list", params, timeout=self.timeout)
            if not isinstance(res, dict):
                raise MCPError("MCP tools/list response must be a JSON object")
            page_tools = res.get("tools", [])
            if not isinstance(page_tools, list):
                raise MCPError("MCP tools/list 'tools' field must be an array")
            for item in page_tools:
                if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
                    tools.append(item)
                    if len(tools) > MAX_REGISTERED_TOOLS:
                        raise MCPError(
                            f"MCP tools/list exceeded the {MAX_REGISTERED_TOOLS}-tool registration limit"
                        )
            next_cursor = res.get("nextCursor")
            if next_cursor in (None, ""):
                cursor = None
                break
            if not isinstance(next_cursor, str) or len(next_cursor) > MAX_PAGE_CURSOR_CHARS:
                raise MCPError("MCP tools/list returned an invalid pagination cursor")
            if next_cursor in seen_cursors:
                raise MCPError("MCP tools/list repeated a pagination cursor; refusing partial results")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            if cursor:
                raise MCPError(
                    f"MCP tools/list exceeded the {MAX_TOOL_PAGES}-page pagination limit; refusing partial results"
                )
        self.tools = tools
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        if not isinstance(tool, str) or not tool.strip():
            raise MCPError("MCP tool name must be a non-empty string")
        if arguments is not None and not isinstance(arguments, dict):
            raise MCPError("MCP tool arguments must be a JSON object")
        res = await self.transport.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}, timeout=self.timeout
        )
        if not isinstance(res, dict):
            return "(invalid MCP tool result: expected a JSON object)"
        text = format_content(res)
        if res.get("isError"):
            return f"ERROR (from MCP tool {self.name}/{tool}): {text}"
        return text

    async def ping(self) -> bool:
        await self.transport.request("ping", None, timeout=10)
        return True

    async def close(self) -> None:
        await self.transport.close()


def _cap_mcp_output(value: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    marker = "\n...[MCP tool output truncated]"
    return value[:max(0, limit - len(marker))] + marker


def format_content(result: Any) -> str:
    """Flatten an MCP CallToolResult safely, with hostile-shape handling and a hard output cap."""
    if not isinstance(result, dict):
        return "(invalid MCP tool result: expected a JSON object)"
    content = result.get("content")
    if content is None:
        content = []
    if not isinstance(content, list):
        return "(invalid MCP tool result: content must be an array)"

    parts: list[str] = []
    for c in content[:MAX_TOOL_OUTPUT_ITEMS]:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        kind = c.get("type")
        if kind == "text":
            value = c.get("text", "")
            parts.append(value if isinstance(value, str) else str(value))
        elif kind == "image":
            data = c.get("data", "")
            try:
                size = len(data) if isinstance(data, (str, bytes, bytearray)) else len(str(data))
            except Exception:
                size = 0
            parts.append(f"[image: {c.get('mimeType', 'unknown')}, {size} b64 chars omitted]")
        elif kind == "audio":
            parts.append(f"[audio: {c.get('mimeType', 'unknown')} omitted]")
        elif kind == "resource":
            resource = c.get("resource")
            if resource is None:
                resource = {}
            if not isinstance(resource, dict):
                parts.append("[resource: malformed payload omitted]")
            elif isinstance(resource.get("text"), str):
                parts.append(f"[resource {resource.get('uri', '')}]\n{resource['text']}")
            elif "text" in resource:
                parts.append("[resource: malformed text omitted]")
            else:
                parts.append(
                    f"[resource: {resource.get('uri', '')} ({resource.get('mimeType', 'binary')}) omitted]"
                )
        elif kind == "resource_link":
            parts.append(f"[resource link: {c.get('uri', '')} {c.get('name', '')}]".strip())
        else:
            parts.append(f"[{kind} content omitted]")
    if len(content) > MAX_TOOL_OUTPUT_ITEMS:
        parts.append(f"[{len(content) - MAX_TOOL_OUTPUT_ITEMS} additional content items omitted]")
    if not parts and result.get("structuredContent") is not None:
        try:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
        except (TypeError, ValueError, RecursionError):
            parts.append("[structured content omitted: not safely serializable]")
    return _cap_mcp_output("\n".join(parts) if parts else "(no content)")


_TYPE_MAP = {"string": "str", "integer": "int", "number": "float", "boolean": "bool",
             "array": "list", "object": "dict", "null": "null"}


def args_doc_from_schema(schema: Any, limit: int = 300) -> str:
    """Create a bounded compact argument doc from possibly untrusted MCP JSON Schema metadata."""
    if not isinstance(schema, dict):
        return "{}"
    if type(limit) is not int or limit < 16:
        limit = 300
    limit = min(limit, 2048)
    props = schema.get("properties")
    if not isinstance(props, dict):
        return "{}"
    required_raw = schema.get("required")
    required = {key for key in required_raw if isinstance(key, str)} if isinstance(required_raw, list) else set()

    items: list[str] = []
    for index, (key, raw_spec) in enumerate(props.items()):
        if index >= MAX_SCHEMA_PROPERTIES:
            items.append('"..."?: any')
            break
        name = str(key)[:80]
        safe_name = json.dumps(name, ensure_ascii=True)
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        raw_type = spec.get("type")
        if isinstance(raw_type, list):
            # Only known JSON Schema primitive types may enter the model-facing tool signature.
            type_names = [
                value for value in raw_type
                if isinstance(value, str) and value in _TYPE_MAP and value != "null"
            ]
            t = "|".join(_TYPE_MAP[value] for value in type_names) or "any"
        elif isinstance(spec.get("enum"), list):
            enum = spec["enum"]
            try:
                t = "|".join(json.dumps(value, ensure_ascii=False, default=str) for value in enum[:6])
            except (TypeError, ValueError, RecursionError):
                t = "any"
        elif isinstance(raw_type, str) and raw_type:
            t = _TYPE_MAP.get(raw_type, "any")
        else:
            t = "any"
        items.append(f'{safe_name}{"" if key in required else "?"}: {t}')
    doc = "{" + ", ".join(items) + "}"
    if len(doc) > limit:
        marker = ", ...}"
        doc = doc[:max(0, limit - len(marker))].rsplit(",", 1)[0] + marker
    return doc


def load_config(path: str | os.PathLike | None = None) -> dict[str, dict[str, Any]]:
    """Read and validate the mcpServers map. Missing file -> {}."""
    p = Path(path or os.getenv("AVRONE_MCP_CONFIG", "./mcp.json"))
    if not p.exists():
        return {}
    with p.open("rb") as handle:
        raw_config = handle.read(MAX_MCP_CONFIG_BYTES + 1)
    if len(raw_config) > MAX_MCP_CONFIG_BYTES:
        raise MCPError(f"MCP config exceeds the {MAX_MCP_CONFIG_BYTES}-byte limit")
    try:
        config_text = raw_config.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MCPError("MCP config must be valid UTF-8 JSON") from exc
    data = _strict_json_loads(config_text or "{}")
    servers = data.get("mcpServers", data.get("servers", {})) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        raise MCPError("MCP config server map must be a JSON object")
    if len(servers) > MAX_MCP_SERVERS:
        raise MCPError(f"MCP config exceeds the {MAX_MCP_SERVERS}-server limit")
    configured: dict[str, dict[str, Any]] = {}
    for key, value in servers.items():
        if not isinstance(value, dict):
            continue
        name = str(key)
        if not name.strip():
            raise MCPError("MCP config server names must be non-empty strings")
        disabled = value.get("disabled", False)
        if not isinstance(disabled, bool):
            raise MCPError(f"MCP config for server {name!r}: 'disabled' must be boolean")
        if not disabled:
            configured[name] = value
    return configured


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
            if not isinstance(servers, dict):
                raise MCPError("MCP server configuration must be a JSON object")
            if len(servers) > MAX_MCP_SERVERS:
                raise MCPError(f"MCP configuration exceeds the {MAX_MCP_SERVERS}-server limit")
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
            remote_name = t["name"]
            raw_description = t.get("description")
            if not isinstance(raw_description, str) or not raw_description.strip():
                raw_description = t.get("title")
            if not isinstance(raw_description, str) or not raw_description.strip():
                raw_description = f"MCP tool {remote_name} on {name}"
            description = raw_description.strip().replace("\n", " ")[:500]
            tool_name = f"mcp__{_safe_part(name)}__{_safe_part(remote_name)}"
            try:
                registry.register(Tool(
                    name=tool_name,
                    description=description,
                    args_doc=args_doc_from_schema(t.get("inputSchema")),
                    fn=self._make_fn(client, remote_name),
                    source=f"mcp:{name}",
                ))
                self._registered.append(tool_name)
                count += 1
            except ValueError as e:
                log.warning("skipping MCP tool %s/%s: %s", name, remote_name, e)
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
