"""Ghost Shell: the surface users and admins touch (web UI, REST, WebSocket HUD)."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .bootstrap import build_core, run_cleanups
from .config import settings
from .trinity import TrinityCore

STATIC = Path(__file__).parent / "static"
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # build_core establishes the environment FIRST (so agents never hit a missing
    # sandbox mid-task), then wires tools, plugins and MCP servers.
    core_, cleanups = await build_core(settings)
    state["core"] = core_
    state["tasks"] = set()
    try:
        yield
    finally:
        for t in list(state["tasks"]):
            t.cancel()
        await run_cleanups(cleanups)


app = FastAPI(title="AVRONE", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def core() -> TrinityCore:
    return state["core"]


def require_admin(token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(403, "Admin access is off. Set AVRONE_ADMIN_TOKEN to enable it.")
    if not isinstance(token, str) or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(401, "Bad admin token")


def _is_loopback_client(request: Request | WebSocket) -> bool:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    if host.lower() in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _same_websocket_origin(origin: str, host_header: str) -> bool:
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit("//" + host_header)
        if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname or not parsed_host.hostname:
            return False
        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
        host_port = parsed_host.port or (443 if parsed_origin.scheme == "https" else 80)
        return parsed_origin.hostname.lower().rstrip(".") == parsed_host.hostname.lower().rstrip(".") and origin_port == host_port
    except ValueError:
        return False


def require_api_access(request: Request | WebSocket, token: str | None) -> None:
    """Allow loopback UX by default; require a token for remote clients in auto mode."""
    mode = os.getenv("AVRONE_API_AUTH", "auto").strip().lower()
    if mode == "off":
        return
    if mode not in {"auto", "always", "off"}:
        mode = "always"
    if mode == "auto" and _is_loopback_client(request):
        return
    expected = os.getenv("AVRONE_API_TOKEN", "") or settings.admin_token
    if not expected:
        raise HTTPException(503, "Remote API access is locked: set AVRONE_API_TOKEN or AVRONE_ADMIN_TOKEN")
    cookies = getattr(request, "cookies", {}) or {}
    provided = token or cookies.get("avrone_api_token")
    if not isinstance(provided, str) or not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "API authentication required")


class ChatIn(BaseModel):
    message: str
    mode: str = "squad"  # squad | chat | deep
    squad_size: int | None = None
    first_answer: str | None = None  # deep mode: the answer to stress-test (else a squad runs first)
    background: bool = False


class TerminalIn(BaseModel):
    cmd: str


class RootDispatchIn(BaseModel):
    operation: str
    service: str


@app.post("/api/auth/session")
async def create_api_session(request: Request, response: Response,
                             x_api_token: str | None = Header(default=None)):
    if os.getenv("AVRONE_API_AUTH", "auto").strip().lower() == "off":
        return {"ok": True, "auth": "off"}
    expected = os.getenv("AVRONE_API_TOKEN", "") or settings.admin_token
    if not expected:
        raise HTTPException(503, "Remote API access is locked: set AVRONE_API_TOKEN or AVRONE_ADMIN_TOKEN")
    if not isinstance(x_api_token, str) or not hmac.compare_digest(x_api_token, expected):
        raise HTTPException(401, "Invalid API token")
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    secure = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        "avrone_api_token", expected, max_age=8 * 60 * 60, httponly=True,
        secure=secure, samesite="strict", path="/",
    )
    return {"ok": True, "expires_in": 8 * 60 * 60}


@app.delete("/api/auth/session")
async def delete_api_session(response: Response):
    response.delete_cookie("avrone_api_token", path="/", httponly=True, samesite="strict")
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    c = core()
    return {
        "runner": c.runner.kind,
        "workspace": str(c.runner.workspace),
        "provider": settings.provider,
        "model": settings.llm_model,
        "backend": settings.llm_base_url if settings.llm_mode != "mock" else "mock",
        "squad_size": settings.squad_size,
        "deep_squad_size": settings.deep_squad_size,
        "max_parallel": settings.max_parallel,
        "tools": len(c.tools.names()),
        "admin_enabled": bool(settings.admin_token),
        "agents": c.active,
    }


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    c = core()
    run_id = uuid.uuid4().hex[:10]
    if body.mode == "squad":
        job = c.squad(body.message, body.squad_size, run_id)
    elif body.mode == "chat":
        job = c.chat(body.message, run_id)
    elif body.mode == "deep":
        job = c.deep(body.message, body.first_answer, body.squad_size, run_id)
    else:
        raise HTTPException(400, f"Unknown mode {body.mode!r}. Use squad, chat or deep.")
    if body.background:  # long-running artifact: return now, watch it on the HUD
        t = asyncio.create_task(job)
        state["tasks"].add(t)
        t.add_done_callback(state["tasks"].discard)
        return {"run_id": run_id, "status": "running"}
    return await job


@app.get("/api/tools")
async def tools(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    return core().tools.list()


@app.get("/api/extensions")
async def extensions(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    return getattr(core(), "extensions", {})


# ------------------------------------------------------------------ fleet
def _fleet():
    f = getattr(core(), "fleet", None)
    if f is None or not f.enabled:
        raise HTTPException(404, "fleet disabled (set AVRONE_FLEET_TOKEN)")
    return f


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value} is not accepted")


async def _verified(request: Request) -> dict:
    f = _fleet()
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > 256_000:
                raise HTTPException(413, "Fleet request body exceeds 256 KiB")
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length") from None
    body = await request.body()
    if len(body) > 256_000:
        raise HTTPException(413, "Fleet request body exceeds 256 KiB")
    try:
        f.verify(request.method, request.url.path, body, {k.lower(): v for k, v in request.headers.items()})
    except PermissionError as e:
        raise HTTPException(401, str(e)) from None
    try:
        payload = json.loads(body or b"{}", parse_constant=_reject_json_constant)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Malformed fleet JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Fleet request body must be a JSON object")
    return payload


@app.get("/api/fleet")
async def fleet_info(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    f = _fleet()
    return {**f.info(), "nodes": f.list_nodes()}


@app.post("/api/fleet/register")
async def fleet_register(request: Request):
    payload = await _verified(request)
    try:
        return _fleet().register(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/fleet/message")
async def fleet_message(request: Request):
    payload = await _verified(request)
    try:
        return await _fleet().handle_message(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except (RuntimeError, KeyError) as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/fleet/directory")
async def fleet_directory(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    from .presence import directory
    return await directory(core())


class AgentMsgIn(BaseModel):
    agent: str
    message: str
    wait: bool = False


@app.post("/api/fleet/tell")
async def fleet_tell(body: AgentMsgIn, x_admin_token: str | None = Header(default=None)):
    """Operator/chatbot -> any agent in the fleet, by name or route/name."""
    require_admin(x_admin_token)
    from .presence import directory, find_agent
    f = _fleet()
    try:
        route, name = find_agent(await directory(core()), body.agent)
        return await f.send_message([p for p in route.split("/") if p], name, body.message,
                                    f"{f.node_id}/operator", 0, body.wait)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (RuntimeError, PermissionError) as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/fleet/jobs")
async def fleet_jobs(job_id: str | None = None, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    f = _fleet()
    if job_id:
        j = await f.queue.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j
    return await f.queue.stats()


@app.post("/api/fleet/run")
async def fleet_run(request: Request):
    payload = await _verified(request)
    f = _fleet()
    try:
        return await f.handle_run(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/gateway")
async def gateway_status(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    return core().llm.status()


@app.get("/api/evolve")
async def api_evolve(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    from .evolve import list_accepted
    return {"accepted": list_accepted(core().settings)}


@app.get("/api/osint/metrics")
async def api_osint_metrics(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    from types import SimpleNamespace
    from .osint import t_osint_metrics
    c = core()
    ctx = SimpleNamespace(core=c, run_id="api", name="api", depth=0)
    return {"metrics": await t_osint_metrics(ctx, {})}


@app.get("/api/telemetry")
async def api_telemetry(request: Request, hours: float = 24, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    from .telemetry import telemetry
    return telemetry(core(), hours * 3600)


@app.get("/api/map")
async def api_map(request: Request, hours: float = 24, format: str = "json", x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    from fastapi.responses import PlainTextResponse
    from .telemetry import system_map, to_mermaid
    g = system_map(core(), hours * 3600)
    return PlainTextResponse(to_mermaid(g)) if format == "mermaid" else g


@app.get("/api/runs")
async def runs(request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    return core().codex.runs()


@app.get("/api/runs/{run_id}")
async def run_log(run_id: str, request: Request, x_api_token: str | None = Header(default=None)):
    require_api_access(request, x_api_token)
    return core().codex.replay(run_id)


@app.post("/api/terminal")
async def terminal(body: TerminalIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    res = await core().runner.run(body.cmd)
    core().codex.emit("admin", "tool.result", "Admin", tool="terminal", output=res.text()[:4000])
    return asdict(res)


@app.post("/api/root/dispatch")
async def root_dispatch(body: RootDispatchIn, x_admin_token: str | None = Header(default=None),
                        x_root_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    from .root_gateway import dispatch_root_operation
    try:
        result = await dispatch_root_operation(body.operation, body.service, x_root_token)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None
    core().codex.emit("admin", "root.dispatch", "Admin", operation=body.operation, service=body.service,
                      exit_code=result.get("exit_code"), duration_s=result.get("duration_s"))
    return result


@app.websocket("/ws/events")
async def events(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and not _same_websocket_origin(origin, ws.headers.get("host", "")):
        await ws.close(code=4403, reason="Cross-origin event stream denied")
        return
    try:
        require_api_access(ws, ws.headers.get("x-api-token"))
    except HTTPException:
        await ws.close(code=4401, reason="API authentication required")
        return
    await ws.accept()
    q = core().codex.subscribe()
    try:
        while True:
            ev = await q.get()
            await ws.send_json(asdict(ev))
    except WebSocketDisconnect:
        pass
    finally:
        core().codex.unsubscribe(q)


def main() -> None:
    import os
    import uvicorn
    uvicorn.run("avrone.server:app", host=os.getenv("AVRONE_HOST", "127.0.0.1"),
                port=int(os.getenv("AVRONE_PORT", "8710")))


if __name__ == "__main__":
    main()
