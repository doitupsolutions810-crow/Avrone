"""Operator-authenticated, explicit Swarm enqueue and exact-request retry API."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .swarm_client import (
    ACTION, AuthorityRejected, CapabilityAuthorityClient,
    DelegationUnknownOutcome, SwarmClient, load_parent_capability_file,
)
from .swarm_outbox import SwarmOutbox, SwarmDispatcher, InvalidOutboxRecord, RequestConflict

_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"


class SwarmEnqueueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=2000, strict=True)
    capability: dict[str, Any] | None = Field(default=None, strict=True)
    capability_signature: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
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
    authority_client_factory: Callable[[], CapabilityAuthorityClient] | None = None,
    parent_capability_loader: Callable[[], tuple[dict[str, Any], str]] | None = None,
):
    """Install explicit enqueue endpoints. Missing trust configuration fails closed."""
    client_factory = client_factory or SwarmClient.from_env
    outbox_factory = outbox_factory or SwarmOutbox
    authority_client_factory = authority_client_factory or CapabilityAuthorityClient.from_env
    parent_capability_loader = parent_capability_loader or load_parent_capability_file
    dispatcher_lock = threading.Lock()

    def dispatcher() -> SwarmDispatcher:
        with dispatcher_lock:
            return initialize_dispatcher()

    def initialize_dispatcher() -> SwarmDispatcher:
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
    def enqueue(body: SwarmEnqueueIn, request: Request,
                      x_admin_token: str | None = Header(default=None)):
        require_local(request, x_admin_token)
        objective = body.objective.strip()
        if not objective:
            raise HTTPException(422, "objective must not be blank")
        try:
            target = dispatcher()
            with target.message_lock(body.message_id):
                if body.message_id:
                    existing = target.outbox.get(body.message_id)
                    if existing is not None:
                        saved = existing["request"]
                        if (saved.get("payload", {}).get("objective") != objective
                                or (body.capability is not None and saved.get("capability") != body.capability)
                                or (body.capability_signature is not None and saved.get("capability_signature") != body.capability_signature)
                                or (body.correlation_id and existing["correlation_id"] != body.correlation_id)):
                            raise HTTPException(409, "message id is already bound to a different request")
                        return response(target.retry(body.message_id))

                if (body.capability is None) != (body.capability_signature is None):
                    raise HTTPException(422, "capability and capability_signature must be supplied together")
                capability, capability_signature = body.capability, body.capability_signature
                if capability is None:
                    if not body.message_id or not body.correlation_id:
                        raise HTTPException(422, "message_id and correlation_id are required for Authority delegation")
                    authority = None
                    try:
                        parent_capability, parent_signature = parent_capability_loader()
                        authority = authority_client_factory()
                        audience = os.getenv("AVRONE_SWARM_AUDIENCE", "spiffe://prod/agent/swarm")
                        try:
                            ttl_seconds = int(os.getenv("AVRONE_SWARM_DELEGATION_TTL_SECONDS", "120"))
                        except ValueError:
                            raise HTTPException(503, "Swarm delegation TTL configuration is invalid") from None
                        if not 1 <= ttl_seconds <= 300:
                            raise HTTPException(503, "Swarm delegation TTL configuration is outside 1..300")
                        delegated = authority.delegate(
                            parent_capability=parent_capability,
                            parent_signature=parent_signature,
                            audience=audience,
                            rights=[dict(ACTION)],
                            ttl_seconds=ttl_seconds,
                            correlation_id=body.correlation_id,
                        )
                        capability, capability_signature = delegated["capability"], delegated["signature"]
                    except DelegationUnknownOutcome:
                        return JSONResponse(status_code=202, content={
                            "state": "delegation_unknown",
                            "correlation_id": body.correlation_id,
                            "message_id": body.message_id,
                            "error_code": "authority_outcome_unknown",
                        })
                    except AuthorityRejected:
                        return JSONResponse(status_code=403, content={
                            "state": "rejected",
                            "correlation_id": body.correlation_id,
                            "message_id": body.message_id,
                            "error_code": "authority_rejected",
                        })
                    except HTTPException:
                        raise
                    except (OSError, RuntimeError):
                        raise HTTPException(503, "Capability Authority trust or parent credential is unavailable") from None
                    except ValueError:
                        raise HTTPException(403, "Capability Authority delegation request was rejected locally") from None
                    finally:
                        if authority is not None:
                            http_client = getattr(authority, "_http", None)
                            if http_client is not None:
                                http_client.close()

                signed = target.client.build_request(
                    payload={"objective": objective}, capability=capability,
                    capability_signature=capability_signature,
                    message_id=body.message_id, correlation_id=body.correlation_id,
                )
                return response(target.submit_request(signed))
        except HTTPException:
            raise
        except RequestConflict:
            raise HTTPException(409, "message id is already bound to a different signed request") from None
        except (ValueError, InvalidOutboxRecord):
            raise HTTPException(403, "capability or signed request was rejected") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Swarm outbox storage is unavailable") from None

    @app.post("/api/swarm/retry/{message_id}")
    def retry(message_id: str, request: Request,
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
    def request_status(message_id: str, request: Request,
                             x_admin_token: str | None = Header(default=None)):
        require_local(request, x_admin_token)
        if not re.fullmatch(_ID_PATTERN, message_id):
            raise HTTPException(400, "invalid message id")
        try:
            record = dispatcher().outbox.get(message_id)
        except InvalidOutboxRecord:
            raise HTTPException(409, "stored Swarm request failed integrity validation") from None
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

    # Compose with the runtime's existing lifespan. Event handlers are not run
    # for an application with a custom lifespan, and newer FastAPI versions no
    # longer expose app.add_event_handler at all.
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def swarm_lifespan(application):
        async with previous_lifespan(application) as lifespan_state:
            try:
                yield lifespan_state
            finally:
                await close_dispatcher()

    app.router.lifespan_context = swarm_lifespan
