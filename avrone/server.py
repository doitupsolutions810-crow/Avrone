"""Ghost Shell: the surface users and admins touch (web UI, REST, WebSocket HUD)."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .codex import Codex
from .config import settings
from .llm import LLM
from .sovereign import establish
from .trinity import TrinityCore

STATIC = Path(__file__).parent / "static"
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Establish the environment FIRST, so agents never hit a missing sandbox mid-task.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    runner = await establish(settings)
    codex = Codex(settings.data_dir)
    state["core"] = TrinityCore(settings, LLM(settings), runner, codex)
    state["tasks"] = set()
    yield
    await runner.stop()


app = FastAPI(title="AVRONE", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def core() -> TrinityCore:
    return state["core"]


def require_admin(token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(403, "Admin access is off. Set AVRONE_ADMIN_TOKEN to enable it.")
    if token != settings.admin_token:
        raise HTTPException(401, "Bad admin token")


class ChatIn(BaseModel):
    message: str
    mode: str = "squad"  # squad | chat
    squad_size: int | None = None
    background: bool = False


class TerminalIn(BaseModel):
    cmd: str


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    c = core()
    return {
        "runner": c.runner.kind,
        "workspace": str(c.runner.workspace),
        "model": settings.llm_model,
        "backend": settings.llm_base_url if settings.llm_mode != "mock" else "mock",
        "squad_size": settings.squad_size,
        "admin_enabled": bool(settings.admin_token),
        "agents": c.active,
    }


@app.post("/api/chat")
async def chat(body: ChatIn):
    c = core()
    run_id = uuid.uuid4().hex[:10]
    job = c.squad(body.message, body.squad_size, run_id) if body.mode == "squad" else c.chat(body.message, run_id)
    if body.background:  # long-running artifact: return now, watch it on the HUD
        t = asyncio.create_task(job)
        state["tasks"].add(t)
        t.add_done_callback(state["tasks"].discard)
        return {"run_id": run_id, "status": "running"}
    return await job


@app.get("/api/runs")
async def runs():
    return core().codex.runs()


@app.get("/api/runs/{run_id}")
async def run_log(run_id: str):
    return core().codex.replay(run_id)


@app.post("/api/terminal")
async def terminal(body: TerminalIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    res = await core().runner.run(body.cmd)
    core().codex.emit("admin", "tool.result", "Admin", tool="terminal", output=res.text()[:4000])
    return asdict(res)


@app.websocket("/ws/events")
async def events(ws: WebSocket):
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
