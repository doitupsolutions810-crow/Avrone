"""Durable, fail-closed persistence for exact signed Swarm enqueue requests."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 512 * 1024
MAX_RECEIPT_BYTES = 128 * 1024
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class OutboxError(RuntimeError):
    pass


class RequestConflict(OutboxError):
    pass


class InvalidOutboxRecord(OutboxError):
    pass


def _json_bytes(value: Any, limit: int) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidOutboxRecord("record is not finite UTF-8 JSON") from exc
    if len(raw) > limit:
        raise InvalidOutboxRecord("record exceeds outbox size limit")
    return raw


def _decode(raw: bytes) -> Any:
    def reject_constant(_value: str):
        raise ValueError("non-finite JSON constant")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                          object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InvalidOutboxRecord("stored outbox record is corrupt") from exc


class SwarmOutbox:
    """SQLite WAL outbox. Each committed request survives process restart.

    Only a receipt already verified by SwarmClient may be passed to mark_queued.
    The outbox does not itself confer authorization or claim remote task completion.
    """

    def __init__(self, path: str | os.PathLike[str]):
        raw_path = Path(path).expanduser()
        raw_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = raw_path.parent.resolve(strict=True)
        if not parent.is_dir():
            raise OutboxError("outbox parent is not a directory")
        self.path = parent / raw_path.name
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OutboxError("outbox database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=5.0,
                                   isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""CREATE TABLE IF NOT EXISTS swarm_outbox (
            message_id TEXT PRIMARY KEY,
            correlation_id TEXT NOT NULL,
            request_json BLOB NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','queued','rejected')),
            receipt_json BLOB,
            receipt_sha256 TEXT,
            error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(swarm_outbox)")}
        if "receipt_sha256" not in columns:
            self._db.execute("ALTER TABLE swarm_outbox ADD COLUMN receipt_sha256 TEXT")
        os.chmod(self.path, 0o600)
        self._fsync_files()

    @classmethod
    def from_env(cls) -> "SwarmOutbox":
        return cls(os.getenv("AVRONE_SWARM_OUTBOX_DB", "./data/swarm-outbox.sqlite3"))

    def get_by_correlation(self, correlation_id: str) -> dict[str, Any] | None:
        if not isinstance(correlation_id, str) or not _ID.fullmatch(correlation_id):
            raise InvalidOutboxRecord("correlation id is invalid")
        with self._lock:
            rows = self._db.execute("SELECT message_id FROM swarm_outbox WHERE correlation_id=? ORDER BY created_at DESC LIMIT 2",
                                    (correlation_id,)).fetchall()
        if len(rows) > 1: raise RequestConflict("correlation id maps to multiple requests")
        return self.get(rows[0][0]) if rows else None

    def _fsync_files(self) -> None:
        for candidate in (self.path, Path(str(self.path) + "-wal")):
            try:
                fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                continue
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fsync(fd)
            finally:
                os.close(fd)
        dfd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    @staticmethod
    def _identity(request: Any) -> tuple[str, str]:
        if not isinstance(request, dict) or set(request) != {
            "envelope", "payload", "capability", "capability_signature"
        }:
            raise InvalidOutboxRecord("signed request shape is invalid")
        envelope = request.get("envelope")
        if not isinstance(envelope, dict):
            raise InvalidOutboxRecord("signed request envelope is invalid")
        if (not isinstance(request.get("payload"), dict)
                or not isinstance(request.get("capability"), dict)
                or not isinstance(request.get("capability_signature"), str)
                or not request["capability_signature"]):
            raise InvalidOutboxRecord("signed request payload or capability is invalid")
        if not isinstance(envelope.get("signature"), str) or not envelope["signature"]:
            raise InvalidOutboxRecord("signed request signature is missing")
        message_id, correlation_id = envelope.get("message_id"), envelope.get("correlation_id")
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            raise InvalidOutboxRecord("message id is invalid")
        if not isinstance(correlation_id, str) or not _ID.fullmatch(correlation_id):
            raise InvalidOutboxRecord("correlation id is invalid")
        return message_id, correlation_id

    def prepare(self, request: dict[str, Any]) -> dict[str, Any]:
        """Persist before network I/O. Repeated identical IDs are idempotent."""
        message_id, correlation_id = self._identity(request)
        raw = _json_bytes(request, MAX_REQUEST_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT request_json, request_sha256, state FROM swarm_outbox WHERE message_id=?",
                    (message_id,)).fetchone()
                if row:
                    if row[1] != digest or bytes(row[0]) != raw:
                        raise RequestConflict("message id already belongs to a different signed request")
                    self._db.execute("COMMIT")
                    return self.get(message_id)
                self._db.execute("""INSERT INTO swarm_outbox
                    (message_id,correlation_id,request_json,request_sha256,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (message_id, correlation_id, raw, digest, "pending", now, now))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            self._fsync_files()
        return self.get(message_id)

    def get(self, message_id: str) -> dict[str, Any] | None:
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            raise InvalidOutboxRecord("message id is invalid")
        with self._lock:
            row = self._db.execute("""SELECT correlation_id,request_json,request_sha256,state,
                receipt_json,receipt_sha256,error_code,created_at,updated_at FROM swarm_outbox WHERE message_id=?""",
                (message_id,)).fetchone()
        if row is None:
            return None
        raw = bytes(row[1])
        if len(raw) > MAX_REQUEST_BYTES or hashlib.sha256(raw).hexdigest() != row[2]:
            raise InvalidOutboxRecord("stored request digest mismatch")
        request = _decode(raw)
        if self._identity(request) != (message_id, row[0]):
            raise InvalidOutboxRecord("stored request identity mismatch")
        receipt = None
        if row[4] is not None:
            receipt_raw = bytes(row[4])
            if (len(receipt_raw) > MAX_RECEIPT_BYTES or not row[5]
                    or hashlib.sha256(receipt_raw).hexdigest() != row[5]):
                raise InvalidOutboxRecord("stored receipt digest mismatch")
            receipt = _decode(receipt_raw)
            if not isinstance(receipt, dict):
                raise InvalidOutboxRecord("stored receipt is not an object")
        elif row[5] is not None:
            raise InvalidOutboxRecord("receipt digest exists without receipt")
        return {"message_id": message_id, "correlation_id": row[0], "request": request,
                "request_sha256": row[2], "state": row[3], "receipt": receipt,
                "error_code": row[6], "created_at": row[7], "updated_at": row[8]}

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        with self._lock:
            ids = [r[0] for r in self._db.execute(
                "SELECT message_id FROM swarm_outbox WHERE state='pending' ORDER BY created_at LIMIT ?",
                (limit,)).fetchall()]
        return [record for mid in ids if (record := self.get(mid)) is not None]

    def mark_queued(self, message_id: str, verified_receipt: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(verified_receipt, dict):
            raise InvalidOutboxRecord("verified receipt must be an object")
        receipt_raw = _json_bytes(verified_receipt, MAX_RECEIPT_BYTES)
        receipt_digest = hashlib.sha256(receipt_raw).hexdigest()
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT state,receipt_json,receipt_sha256 FROM swarm_outbox WHERE message_id=?",
                                       (message_id,)).fetchone()
                if row is None:
                    raise KeyError(message_id)
                if row[0] == "queued":
                    if bytes(row[1]) != receipt_raw or row[2] != receipt_digest:
                        raise RequestConflict("message already has a different verified receipt")
                elif row[0] != "pending":
                    raise OutboxError("only pending requests can become queued")
                else:
                    self._db.execute("UPDATE swarm_outbox SET state='queued',receipt_json=?,receipt_sha256=?,updated_at=? WHERE message_id=?",
                                     (receipt_raw, receipt_digest, now, message_id))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            self._fsync_files()
        return self.get(message_id)

    def mark_rejected(self, message_id: str, error_code: str) -> dict[str, Any]:
        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error_code):
            raise ValueError("error_code is invalid")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT state FROM swarm_outbox WHERE message_id=?", (message_id,)).fetchone()
                if row is None:
                    raise KeyError(message_id)
                if row[0] == "queued":
                    raise OutboxError("queued request cannot become rejected")
                self._db.execute("UPDATE swarm_outbox SET state='rejected',error_code=?,updated_at=? WHERE message_id=?",
                                 (error_code, time.time(), message_id))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            self._fsync_files()
        return self.get(message_id)

    def close(self) -> None:
        with self._lock:
            self._db.close()


class SwarmDispatcher:
    """Persist-before-send dispatcher. Unknown outcomes remain retryable, never local-fallback."""

    def __init__(self, client: Any, outbox: SwarmOutbox):
        self.client, self.outbox = client, outbox

    def submit_request(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self.outbox.prepare(request)
        if record["state"] == "queued":
            return self._queued_result(record)
        if record["state"] == "rejected":
            return {"state": "rejected", "message_id": record["message_id"],
                    "error_code": record["error_code"]}
        return self._send(record)

    def retry(self, message_id: str) -> dict[str, Any]:
        record = self.outbox.get(message_id)
        if record is None:
            raise KeyError(message_id)
        if record["state"] == "queued":
            return self._queued_result(record)
        if record["state"] == "rejected":
            return {"state": "rejected", "message_id": message_id,
                    "error_code": record["error_code"]}
        return self._send(record)

    @staticmethod
    def _queued_result(record: dict[str, Any]) -> dict[str, Any]:
        return {"state": "queued", "message_id": record["message_id"],
                "correlation_id": record["correlation_id"], "receipt": record["receipt"]}

    def _send(self, record: dict[str, Any]) -> dict[str, Any]:
        from .swarm_client import UnknownOutcome, SwarmRejected
        request = record["request"]
        message_id = record["message_id"]
        try:
            response = self.client.send_request(request)
        except UnknownOutcome:
            return {"state": "unknown", "message_id": message_id,
                    "correlation_id": record["correlation_id"], "retryable": True}
        except SwarmRejected as exc:
            self.outbox.mark_rejected(message_id, f"http_{exc.status_code}")
            return {"state": "rejected", "message_id": message_id,
                    "error_code": f"http_{exc.status_code}"}

        if not isinstance(response, dict) or not isinstance(response.get("job"), dict):
            return {"state": "unknown", "message_id": message_id,
                    "correlation_id": record["correlation_id"], "retryable": True}
        job = response["job"]
        if not isinstance(job.get("id"), str) or not job["id"].strip():
            return {"state": "unknown", "message_id": message_id,
                    "correlation_id": record["correlation_id"], "retryable": True}
        receipt = response.get("receipt")
        self.client.verify_receipt(receipt, request)
        self.outbox.mark_queued(message_id, receipt)
        return {"state": "queued", "message_id": message_id,
                "correlation_id": record["correlation_id"], "job_id": job["id"],
                "receipt": receipt, "note": "Queue acceptance is not task completion."}
