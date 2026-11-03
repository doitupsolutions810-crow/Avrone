"""Shared Trinity/API submission boundary: durable intent, exact replay, no local fallback."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, Callable

from .swarm_client import ACTION, CapabilityAuthorityClient, SwarmClient, load_parent_capability_file
from .swarm_outbox import RequestConflict, SwarmDispatcher, SwarmOutbox


def delegation_ttl() -> int:
    try:
        value = int(os.getenv("AVRONE_SWARM_DELEGATION_TTL_SECONDS", "120"))
    except ValueError:
        raise RuntimeError("Swarm delegation TTL configuration is invalid") from None
    if not 1 <= value <= 300:
        raise RuntimeError("Swarm delegation TTL configuration is outside 1..300")
    return value


def _close_http(client: Any) -> None:
    http = getattr(client, "_http", None)
    if http is not None:
        http.close()


@contextmanager
def open_dispatcher(data_dir: Path):
    client = SwarmClient.from_env()
    box = None
    try:
        path = Path(os.getenv("AVRONE_SWARM_OUTBOX_DB") or data_dir / "swarm-outbox.sqlite3")
        box = SwarmOutbox(path)
        yield SwarmDispatcher(client, box)
    finally:
        try:
            if box is not None:
                box.close()
        finally:
            _close_http(client)


def submit_objective(
    target: SwarmDispatcher, objective: str, message_id: str, correlation_id: str, *,
    authority_factory: Callable[[], Any] | None = None,
    parent_loader: Callable[[], tuple[dict, str]] | None = None,
) -> dict[str, Any]:
    """Persist intent/assertion before I/O and resume without rereading a parent file.

    SQLite transactions bind IDs across independent callers. Concurrent minting
    uses the first persisted assertion. Only the winning signed enqueue request
    is ever sent; durable dispatch leases fence its network attempts.
    """
    objective = objective.strip()
    target.outbox.bind_submission(message_id, correlation_id, objective)

    def existing_request():
        saved = target.outbox.get_by_correlation(correlation_id)
        if saved is None:
            saved = target.outbox.get(message_id)
        if saved is not None:
            if (saved["message_id"] != message_id or saved["correlation_id"] != correlation_id
                    or saved["request"].get("payload") != {"objective": objective}):
                raise RequestConflict("submission identity already belongs to a different objective")
        return saved

    if existing_request() is not None:
        return target.retry(message_id)

    authority_factory = authority_factory or CapabilityAuthorityClient.from_env
    parent_loader = parent_loader or load_parent_capability_file
    saved = target.outbox.get_authority_request(correlation_id)
    replay = saved is not None
    authority = authority_factory()
    try:
        if saved is None:
            parent, signature = parent_loader()
            request = {
                "parent_capability": parent, "parent_signature": signature,
                "audience": target.client.audience,
                "rights": [dict(ACTION)], "ttl_seconds": delegation_ttl(),
                "correlation_id": correlation_id, "requester_assertion": authority._assertion(),
            }
            try:
                saved = target.outbox.prepare_authority_request(correlation_id, request)
            except RequestConflict:
                saved = target.outbox.get_authority_request(correlation_id)
                if saved is None:
                    raise
                replay = True
        if saved["request"].get("audience") != target.client.audience:
            raise RequestConflict("persisted Authority request belongs to another Swarm audience")
        delegated = authority.delegate(**saved["request"], allow_expired_parent_replay=replay)
    finally:
        _close_http(authority)

    # Another process may have persisted the winning envelope while this one
    # waited for its exact-idempotent delegation response.
    if existing_request() is not None:
        return target.retry(message_id)
    signed = target.client.build_request(
        payload={"objective": objective}, capability=delegated["capability"],
        capability_signature=delegated["signature"],
        message_id=message_id, correlation_id=correlation_id,
    )
    try:
        return target.submit_request(signed)
    except RequestConflict:
        if existing_request() is None:
            raise
        return target.retry(message_id)
