from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import rfc8785

from avrone.swarm_client import (
    ACTION,
    AuthorityRejected,
    CapabilityAuthorityClient,
    DelegationUnknownOutcome,
    ReplayConflict,
    SwarmClient,
    SwarmRejected,
    UnknownOutcome,
)


class SwarmOutboxError(RuntimeError):
    pass


class SwarmOutbox:
    """Mode-0600 durable outbox; ambiguous retries reuse the exact signed envelope."""

    SCHEMA = "avrone.swarm-outbox/v1"
    MAX_RECORD_BYTES = 1_048_576

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or stat.S_IMODE(self.directory.stat().st_mode) & 0o077:
            raise SwarmOutboxError("Swarm outbox directory must be a real directory with mode 0700 or stricter")

    @classmethod
    def from_env(cls) -> "SwarmOutbox":
        return cls(os.getenv("AVRONE_SWARM_OUTBOX_DIR", "~/.avrone/swarm-outbox"))

    @staticmethod
    def _key(correlation_id: str) -> str:
        if not isinstance(correlation_id, str) or not correlation_id.strip() or len(correlation_id) > 128:
            raise ValueError("correlation_id must contain 1..128 characters")
        return hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()

    @contextmanager
    def _lock(self, key: str):
        path = self.directory / (key + ".lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a+b", closefd=False) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self, path: Path, correlation_id: str) -> dict[str, Any] | None:
        if path.is_symlink():
            raise SwarmOutboxError("outbox record must not be a symlink")
        if not path.exists():
            return None
        if not path.is_file():
            raise SwarmOutboxError("outbox record must be a regular file")
        if path.stat().st_size > self.MAX_RECORD_BYTES:
            raise SwarmOutboxError("outbox record exceeds size limit")
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SwarmOutboxError("outbox record is unreadable or malformed") from exc
        if not isinstance(value, dict) or value.get("schema") != self.SCHEMA or value.get("correlation_id") != correlation_id:
            raise SwarmOutboxError("outbox record identity/schema mismatch")
        return value

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > self.MAX_RECORD_BYTES:
            raise SwarmOutboxError("outbox record exceeds size limit")
        fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=self.directory)
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            os.chmod(path, 0o600)
            dir_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def submit(
        self, *, task: str, correlation_id: str,
        parent_capability: dict[str, Any], parent_signature: str,
        authority_client: CapabilityAuthorityClient, swarm_client: SwarmClient,
        delegation_ttl_seconds: int = 120,
    ) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip() or len(task) > 2000:
            raise ValueError("task must contain 1..2000 characters")
        key = self._key(correlation_id)
        path = self.directory / (key + ".json")
        request_digest = "sha256:" + hashlib.sha256(rfc8785.dumps({
            "task": task,
            "parent_capability": parent_capability,
            "parent_signature": parent_signature,
            "delegation_ttl_seconds": delegation_ttl_seconds,
            "audience": swarm_client.audience,
            "action": ACTION,
        })).hexdigest()
        with self._lock(key):
            record = self._read(path, correlation_id) or {
                "schema": self.SCHEMA,
                "correlation_id": correlation_id,
                "request_digest": request_digest,
                "state": "prepared",
                "task": task,
                "parent_capability": parent_capability,
                "parent_signature": parent_signature,
                "delegation_ttl_seconds": delegation_ttl_seconds,
            }
            if record.get("request_digest") != request_digest:
                raise SwarmOutboxError("correlation_id already belongs to a different task or parent capability")
            if record.get("state") == "queued":
                try:
                    stored_request = record["request"]
                    stored_result = record["result"]
                    if stored_request["envelope"]["correlation_id"] != correlation_id:
                        raise ValueError("outbox request correlation mismatch")
                    swarm_client.verify_request(stored_request)
                    swarm_client.verify_receipt(stored_result["receipt"], stored_request)
                    if not isinstance(stored_result.get("job"), dict) or not isinstance(stored_result["job"].get("id"), str):
                        raise ValueError("stored job receipt is incomplete")
                except (KeyError, TypeError, ValueError) as exc:
                    raise SwarmOutboxError("stored queued result failed independent verification") from exc
                return dict(stored_result, status="queued", idempotent_replay=True, outbox_path=str(path))
            if record.get("state") == "rejected":
                return {"status": "rejected", "correlation_id": correlation_id, "reason": record.get("last_error", "previously rejected"), "outbox_path": str(path)}

            if not isinstance(record.get("delegated_capability"), dict):
                try:
                    delegated = authority_client.delegate(
                        parent_capability=parent_capability,
                        parent_signature=parent_signature,
                        audience=swarm_client.audience,
                        rights=[dict(ACTION)],
                        ttl_seconds=delegation_ttl_seconds,
                        correlation_id=correlation_id,
                    )
                except DelegationUnknownOutcome as exc:
                    record.update(state="delegation_unknown", last_error=str(exc))
                    self._write(path, record)
                    return {"status": "pending", "stage": "delegation", "correlation_id": correlation_id, "reason": str(exc), "outbox_path": str(path)}
                except AuthorityRejected as exc:
                    record.update(state="rejected", last_error=str(exc))
                    self._write(path, record)
                    return {"status": "rejected", "stage": "delegation", "correlation_id": correlation_id, "reason": str(exc), "outbox_path": str(path)}
                record["delegated_capability"] = delegated["capability"]
                record["delegated_signature"] = delegated["signature"]
                record["state"] = "delegated"
                self._write(path, record)

            if not isinstance(record.get("request"), dict):
                record["request"] = swarm_client.build_request(
                    payload={"objective": task},
                    capability=record["delegated_capability"],
                    capability_signature=record["delegated_signature"],
                    correlation_id=correlation_id,
                )
                record["state"] = "prepared_send"
                self._write(path, record)

            try:
                result = swarm_client.send_request(record["request"])
            except UnknownOutcome as exc:
                record.update(state="submission_unknown", last_error=str(exc))
                self._write(path, record)
                return {"status": "pending", "stage": "enqueue", "correlation_id": correlation_id, "message_id": record["request"]["envelope"]["message_id"], "reason": str(exc), "outbox_path": str(path)}
            except SwarmRejected as exc:
                record.update(state="rejected", last_error=str(exc))
                self._write(path, record)
                return {"status": "rejected", "stage": "enqueue", "correlation_id": correlation_id, "reason": str(exc), "outbox_path": str(path)}
            record.update(state="queued", result=result, last_error=None)
            self._write(path, record)
            return dict(result, status="queued", idempotent_replay=False, outbox_path=str(path))

