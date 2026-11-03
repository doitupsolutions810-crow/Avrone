"""Operator-authenticated, explicit Swarm enqueue and exact-request retry API."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .swarm_client import SwarmClient
from .swarm_outbox import SwarmOutbox, SwarmDispatcher, InvalidOutboxRecord

_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"


class SwarmEnqueueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=2000, strict=True)
    capability: dict[str, Any] = Field(strict=True)
    capability_signature: str = Field(min_length=1, max_length=256, strict=True)
    message_id: str | None = Field(default=None, pattern=_ID_PATTERN, strict=True)
    correlation_id: str | None = Field(default=None, pattern=_ID_PATTERN, strict=True)


def install_routes(
    app,
    get_core: Callable[[], Any],
    require_local: Callable[[Request, str | None], Any],
    settings,
    *,
    client_factory: Callable[[], SwarmClient] | None = None,
    outbox_factory: Callable[[Path], SwarmOutbox] | None = None,
):
    """Install explicit enqueue endpoints. Missing trust configuration fails closed."""
    client_factory = client_factory or SwarmClient.from_env
    outbox_factory = outbox_factory or SwarmOutbox

    def dispatcher() -> SwarmDispatcher:
        current = getattr(app.state, "swarm_dispatcher", None)
        if current is not None:
            return current
        client = None
        try:
            client = client_factory()
            outbox = outbox_factory(Path(settings.data_dir) / "swarm-outbox.sqlite3")
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            if client is not None:
                try:
                    client._http.close()
                except Exception:
                    pass
            raise HTTPException(503, "Swarm dispatch is not configured or durable storage is unavailable") from None
        current = SwarmDispatcher(client, outbox)
        app.state.swarm_dispatcher = current
        return current

    def response(result: dict[str, Any]) -> JSONResponse:
        if result.get("state") == "rejected":
            return JSONResponse(status_code=403, content=result)
        # 202 means the enqueue was accepted or its outcome is unknown. Neither means completed.
        return JSONResponse(status_code=202, content=result)

    @app.post("/api/swarm/enqueue")
    async def enqueue(body: SwarmEnqueueIn, request: Request,
                      x_admin_token: str | None = Header(default=None)):
        require_local(request, x_admin_token)
        objective = body.objective.strip()
        if not objective:
            raise HTTPException(422, "objective must not be blank")
        try:
            target = dispatcher()
            if body.message_id:
                existing = target.outbox.get(body.message_id)
                if existing is not None:
                    saved = existing["request"]
                    if (saved.get("payload", {}).get("objective") != objective
                            or saved.get("capability") != body.capability
                            or saved.get("capability_signature") != body.capability_signature
                            or (body.correlation_id and existing["correlation_id"] != body.correlation_id)):
                        raise HTTPException(409, "message id is already bound to a different request")
                    return response(target.retry(body.message_id))
            signed = target.client.build_request(
                payload={"objective": objective}, capability=body.capability,
                capability_signature=body.capability_signature,
                message_id=body.message_id, correlation_id=body.correlation_id,
            )
            return response(target.submit_request(signed))
        except HTTPException:
            raise
        except (ValueError, InvalidOutboxRecord):
            raise HTTPException(403, "capability or signed request was rejected") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Swarm outbox storage is unavailable") from None

    @app.post("/api/swarm/retry/{message_id}")
    async def retry(message_id: str, request: Request,
                    x_admin_token: str | None = Header(default=None)):
        require_local(request, x_admin_token)
        if not re.fullmatch(_ID_PATTERN, message_id):
            raise HTTPException(400, "invalid message id")
        target = dispatcher()
        try:
            result = target.retry(message_id)
        except KeyError:
            raise HTTPException(404, "Swarm request not found") from None
        except InvalidOutboxRecord:
            raise HTTPException(409, "stored Swarm request failed integrity validation") from None
        except sqlite3.Error:
            raise HTTPException(503, "Swarm outbox storage is unavailable") from None
        return response(result)

    @app.get("/api/swarm/requests/{message_id}")
    async def request_status(message_id: str, request: Request,
                             x_admin_token: str | None = Header(default=None)):
        require_local(request, x_admin_token)
        if not re.fullmatch(_ID_PATTERN, message_id):
            raise HTTPException(400, "invalid message id")
        try:
            record = dispatcher().outbox.get(message_id)
        except sqlite3.Error:
            raise HTTPException(503, "Swarm outbox storage is unavailable") from None
        if record is None:
            raise HTTPException(404, "Swarm request not found")
        return {key: record[key] for key in ("message_id", "correlation_id", "state", "request_sha256", "receipt", "error_code")}

    async def close_dispatcher():
        current = getattr(app.state, "swarm_dispatcher", None)
        if current is not None:
            try:
                current.outbox.close()
            finally:
                http_client = getattr(current.client, "_http", None)
                if http_client is not None:
                    http_client.close()
            app.state.swarm_dispatcher = None

    app.add_event_handler("shutdown", close_dispatcher)
