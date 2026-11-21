"""HTTP API for the Crow-OS AVRONE Workspace."""

from __future__ import annotations

import asyncio
import json
import os
import queue
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .db import Store
from .gitrepo import GitError, repo_root_from
from .runner import RunnerBusy
from .service import Workspace
from .status import VerificationRejected

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
STATIC = PACKAGE_ROOT / "static"


def default_repo() -> Path:
    env = os.environ.get("CROW_REPO")
    if env:
        return Path(env).resolve()
    found = repo_root_from(PACKAGE_ROOT)
    if found is None:
        raise RuntimeError("Set CROW_REPO to the Avrone checkout")
    return found


def create_app(
    repo_root: Path | None = None,
    db_path: Path | None = None,
    worktree_root: Path | None = None,
    ingest_on_startup: bool = True,
) -> FastAPI:
    repo = Path(repo_root).resolve() if repo_root else default_repo()
    home = Path(os.environ.get("CROW_HOME") or PACKAGE_ROOT)
    database = Path(db_path) if db_path else home / "var" / "crow.sqlite"
    trees = Path(worktree_root) if worktree_root else Path(os.environ.get("CROW_WORKTREES") or "/tmp/crow-os-worktrees")
    store = Store(database)
    workspace = Workspace(store, repo, trees)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.ingest_on_startup:
            await asyncio.to_thread(app.state.workspace.ingest)
        await asyncio.to_thread(app.state.workspace.recover)
        yield

    app = FastAPI(title="Crow-OS AVRONE Workspace", lifespan=lifespan)
    app.state.workspace = workspace
    app.state.ingest_on_startup = ingest_on_startup

    @app.exception_handler(VerificationRejected)
    async def _rejected(_request: Request, exc: VerificationRejected):
        return JSONResponse(status_code=409, content={"error": "verification rejected", "reasons": exc.reasons})

    @app.exception_handler(PermissionError)
    async def _forbidden(_request: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"error": str(exc)})

    @app.exception_handler(RunnerBusy)
    async def _busy(_request: Request, exc: RunnerBusy):
        return JSONResponse(status_code=409, content={"error": f"round {exc} is already running"})

    @app.exception_handler(GitError)
    async def _git(_request: Request, exc: GitError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(KeyError)
    async def _missing(_request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"error": f"not found: {exc.args[0] if exc.args else ''}"})

    @app.exception_handler(ValueError)
    async def _bad(_request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/api/health")
    def health():
        return {"ok": True, "repo": str(workspace.repo)}

    @app.get("/api/overview")
    def overview():
        return workspace.overview()

    @app.post("/api/ingest")
    def refresh():
        return workspace.ingest()

    @app.get("/api/branches")
    def branches():
        return {"branches": workspace.branches()}

    @app.get("/api/commits")
    def commits(branch: str | None = None):
        return {"commits": workspace.commits(branch)}

    @app.get("/api/pulls")
    def pulls():
        github = workspace.store.meta_get("github") or {}
        return {"github": github, "pulls": workspace.pulls()}

    @app.get("/api/checks")
    def checks():
        github = workspace.store.meta_get("github") or {}
        return {"github": github, "checks": workspace.checks()}

    @app.get("/api/suites")
    def suites(ref: str | None = None, suite: str | None = None):
        return {"files": workspace.suites(ref, suite)}

    @app.get("/api/artifacts")
    def artifacts(ref: str | None = None, kind: str | None = None):
        return {"artifacts": workspace.artifacts(ref, kind)}

    @app.get("/api/artifacts/text")
    def artifact_text(ref: str, path: str):
        return workspace.artifact_text(ref, path)

    @app.get("/api/changes")
    def changes():
        return {"changes": workspace.changes()}

    @app.post("/api/changes")
    async def create_change(request: Request):
        body = await request.json()
        return workspace.create_change(
            title=body.get("title") or "",
            ref=body.get("ref") or "",
            required_files=list(body.get("required_files") or []),
        )

    @app.post("/api/changes/{change_id}/gates/{gate_id}")
    async def attest(change_id: str, gate_id: str, request: Request):
        body = await request.json()
        return workspace.attest_gate(change_id, gate_id, bool(body.get("passed")))

    @app.post("/api/changes/{change_id}/verify")
    def verify(change_id: str):
        return workspace.verify(change_id)

    @app.get("/api/rounds")
    def rounds():
        return {"rounds": workspace.rounds()}

    @app.post("/api/rounds")
    async def launch(request: Request):
        body = await request.json()
        return workspace.launch_round(
            title=body.get("title") or "",
            ref=body.get("ref") or "",
            files=list(body.get("files") or []),
            runner=body.get("runner") or "python",
            change_id=body.get("change_id") or None,
            acceptance=bool(body.get("acceptance")),
            background=True,
        )

    @app.get("/api/rounds/{round_id}")
    def round_detail(round_id: str):
        return workspace.round_detail(round_id)

    @app.post("/api/rounds/{round_id}/cancel")
    def cancel(round_id: str):
        return workspace.cancel_round(round_id)

    @app.post("/api/rounds/{round_id}/resume")
    def resume(round_id: str):
        return workspace.resume_round(round_id, background=True)

    @app.get("/api/rounds/{round_id}/stream")
    def stream(round_id: str):
        workspace.round_detail(round_id)
        subscriber = workspace.subscribe(round_id)

        async def generate():
            try:
                current = workspace.round_detail(round_id)
                yield _sse({"type": "status", "status": current["status"], "replay": True})
                if current["status"] in {"passed", "failed", "advisory", "cancelled"} and current.get("sealed"):
                    return
                while True:
                    event = await asyncio.to_thread(_get, subscriber)
                    yield _sse(event)
                    if event.get("type") == "status" and event.get("status") in {
                        "passed",
                        "failed",
                        "advisory",
                        "cancelled",
                        "interrupted",
                    }:
                        return
            finally:
                workspace.unsubscribe(round_id, subscriber)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/compare")
    async def compare(request: Request):
        body = await request.json()
        return workspace.compare(body.get("round_a") or "", body.get("round_b") or "")

    @app.get("/api/fixes")
    def fixes():
        return {"fixes": workspace.fixes()}

    @app.post("/api/fixes/{fix_id}/link")
    async def link_fix(fix_id: str, request: Request):
        body = await request.json()
        pr = body.get("pr")
        return workspace.link_fix(fix_id, commit=body.get("commit") or None, pr=int(pr) if pr else None)

    @app.get("/api/assignments")
    def assignments():
        return {"assignments": workspace.assignments(), "lanes": ["coverage", "tools", "skills", "robustness"]}

    @app.post("/api/assignments")
    async def assign(request: Request):
        body = await request.json()
        return workspace.assign(
            lane=body.get("lane") or "",
            agent_kind=body.get("agent_kind") or "",
            agent_ref=body.get("agent_ref") or "",
            note=body.get("note") or "",
            change_id=body.get("change_id") or None,
        )

    @app.get("/api/checkpoints")
    def checkpoints():
        return {"checkpoints": workspace.checkpoints()}

    @app.get("/api/propagation")
    def propagation():
        return workspace.propagation()

    @app.get("/api/events")
    def events(limit: int = 200):
        return {"events": workspace.events(min(limit, 500))}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _get(subscriber: queue.Queue):
    try:
        return subscriber.get(timeout=12)
    except queue.Empty:
        return {"type": "ping"}
