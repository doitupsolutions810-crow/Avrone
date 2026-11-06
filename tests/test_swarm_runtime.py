"""Shared submission boundary: real SQLite, explicit wire-contract test doubles."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from avrone import swarm_runtime
from avrone.swarm_client import AuthorityRejected, DelegationUnknownOutcome
from avrone.swarm_outbox import InvalidOutboxRecord, RequestConflict, SwarmDispatcher, SwarmOutbox
from tests.test_swarm_api import FakeAuthority, FakeClient


def dispatcher(path, client=None):
    return SwarmDispatcher(client or FakeClient(), SwarmOutbox(path))


def forbidden():
    raise AssertionError("retry must not read a new parent or mint an assertion")


def test_authority_timeout_survives_restart_without_parent_reload(tmp_path):
    path = tmp_path / "outbox.db"
    first = dispatcher(path)
    authority = FakeAuthority(DelegationUnknownOutcome("timeout", {"correlation_id": "corr"}))
    closed = []
    authority._http = SimpleNamespace(close=lambda: closed.append(True))
    with pytest.raises(DelegationUnknownOutcome):
        swarm_runtime.submit_objective(first, "objective", "message", "corr",
            authority_factory=lambda: authority, parent_loader=lambda: ({"id": "parent"}, "signature"))
    request = first.outbox.get_authority_request("corr")["request"]
    assert first.outbox.get("message") is None and closed == [True]
    first.outbox.close()
    second = dispatcher(path)
    recovered = FakeAuthority()
    recovered._assertion = forbidden
    try:
        result = swarm_runtime.submit_objective(second, "objective", "message", "corr",
            authority_factory=lambda: recovered, parent_loader=forbidden)
        assert result["state"] == "queued"
        assert recovered.calls == [{**request, "allow_expired_parent_replay": True}]
    finally:
        second.outbox.close()


@pytest.mark.parametrize("message,correlation,objective", [
    ("message", "corr", "another task"), ("another", "corr", "objective"),
    ("message", "another", "objective"),
])
def test_ids_cannot_be_rebound_after_delegation_timeout(tmp_path, message, correlation, objective):
    target = dispatcher(tmp_path / "outbox.db")
    authority = FakeAuthority(DelegationUnknownOutcome("timeout", {"correlation_id": "corr"}))
    try:
        with pytest.raises(DelegationUnknownOutcome):
            swarm_runtime.submit_objective(target, "objective", "message", "corr",
                authority_factory=lambda: authority, parent_loader=lambda: ({"id": "p"}, "s"))
        with pytest.raises(RequestConflict):
            swarm_runtime.submit_objective(target, objective, message, correlation,
                authority_factory=forbidden, parent_loader=forbidden)
        assert not target.client.calls
    finally:
        target.outbox.close()


def test_pending_enqueue_retry_does_not_reconstruct_authority(tmp_path):
    target = dispatcher(tmp_path / "outbox.db", FakeClient(fail_first=True))
    try:
        first = swarm_runtime.submit_objective(target, "objective", "message", "corr",
            authority_factory=FakeAuthority, parent_loader=lambda: ({"id": "p"}, "s"))
        assert first["state"] == "unknown"
        second = swarm_runtime.submit_objective(target, "objective", "message", "corr",
            authority_factory=forbidden, parent_loader=forbidden)
        assert second["state"] == "queued"
        assert target.client.calls[0] == target.client.calls[1]
    finally:
        target.outbox.close()


def test_independent_connections_share_assertion_and_send_one_winning_envelope(tmp_path):
    path = tmp_path / "outbox.db"
    targets = [dispatcher(path) for _ in range(4)]
    authorities = []
    barrier = threading.Barrier(4)
    build_barrier = threading.Barrier(4)
    entered, release = threading.Event(), threading.Event()
    sent = []

    class Authority(FakeAuthority):
        def _assertion(self):
            return {"signature": "contract-signature", "jti": str(threading.get_ident())}

        def delegate(self, **kwargs):
            result = super().delegate(**kwargs)
            barrier.wait(timeout=6)
            return result

    def submit(target):
        authority = Authority()
        authorities.append(authority)
        original = target.client.send_request
        original_build = target.client.build_request

        def build(**kwargs):
            request = original_build(**kwargs)
            request["envelope"]["nonce"] = str(threading.get_ident())
            build_barrier.wait(timeout=6)
            return request

        def send(request):
            sent.append(request)
            entered.set()
            assert release.wait(6)
            return original(request)

        target.client.send_request = send
        target.client.build_request = build
        return swarm_runtime.submit_objective(target, "objective", "message", "corr",
            authority_factory=lambda: authority, parent_loader=lambda: ({"id": "p"}, "s"))

    try:
        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(submit, target) for target in targets]
            try:
                assert entered.wait(6)
            finally:
                release.set()
            results = [future.result(timeout=8) for future in futures]
        assert len(sent) == 1
        assert {result["state"] for result in results} <= {"unknown", "queued"}
        assertions = [authority.calls[0]["requester_assertion"] for authority in authorities]
        assert all(assertion == assertions[0] for assertion in assertions)
        assert targets[0].outbox.get("message")["state"] == "queued"
    finally:
        for target in targets:
            target.outbox.close()


def test_audience_change_cannot_retarget_persisted_delegation(tmp_path):
    target = dispatcher(tmp_path / "outbox.db")
    authority = FakeAuthority(DelegationUnknownOutcome("timeout", {"correlation_id": "corr"}))
    try:
        with pytest.raises(DelegationUnknownOutcome):
            swarm_runtime.submit_objective(target, "objective", "message", "corr",
                authority_factory=lambda: authority, parent_loader=lambda: ({"id": "p"}, "s"))
        target.client.audience = "another-swarm"
        with pytest.raises(RequestConflict, match="audience"):
            swarm_runtime.submit_objective(target, "objective", "message", "corr",
                authority_factory=FakeAuthority, parent_loader=forbidden)
    finally:
        target.outbox.close()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e999", '"\\ud800"'])
def test_even_rehashed_invalid_json_in_authority_store_is_refused(tmp_path, bad):
    target = dispatcher(tmp_path / "outbox.db")
    raw = ('{"correlation_id":"corr","value":' + bad + '}').encode()
    target.outbox._db.execute("INSERT INTO authority_delegations VALUES(?,?,?,?,?)",
                             ("corr", raw, hashlib.sha256(raw).hexdigest(), 0, 0))
    try:
        with pytest.raises(InvalidOutboxRecord):
            target.outbox.get_authority_request("corr")
    finally:
        target.outbox.close()


def test_legacy_schema_migration_is_serialized_across_connections(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE swarm_outbox(message_id TEXT PRIMARY KEY,correlation_id TEXT,
            request_json BLOB,request_sha256 TEXT,state TEXT,receipt_json BLOB,error_code TEXT,
            created_at REAL,updated_at REAL)""")
        db.execute("PRAGMA journal_mode=WAL")
    barrier = threading.Barrier(4)

    def open_box(_):
        barrier.wait(timeout=5)
        box = SwarmOutbox(path)
        try:
            return {row[1] for row in box._db.execute("PRAGMA table_info(swarm_outbox)")}
        finally:
            box.close()

    with ThreadPoolExecutor(4) as pool:
        assert all("receipt_sha256" in columns for columns in pool.map(open_box, range(4)))


def test_dispatcher_closes_client_when_database_open_fails(tmp_path, monkeypatch):
    closed = []
    client = FakeClient()
    client._http = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(swarm_runtime.SwarmClient, "from_env", lambda: client)
    def fail(path):
        raise OSError("no disk")
    monkeypatch.setattr(swarm_runtime, "SwarmOutbox", fail)
    with pytest.raises(OSError):
        with swarm_runtime.open_dispatcher(tmp_path):
            raise AssertionError("unavailable storage must not yield a dispatcher")
    assert closed == [True]


def test_checkpoint_binding_survives_connection_restart_and_cannot_be_rebound(tmp_path):
    path = tmp_path / "outbox.db"
    first = SwarmOutbox(path)
    assert first.checkpoint_correlation("scope", "objective", correlation_id="chosen", default_correlation_id="default") == "chosen"
    first.close()
    second = SwarmOutbox(path)
    try:
        assert second.checkpoint_correlation("scope", "objective", correlation_id=None, default_correlation_id="new-run") == "chosen"
        with pytest.raises(RequestConflict):
            second.checkpoint_correlation("scope", "objective", correlation_id="new-id", default_correlation_id="new-run")
        with pytest.raises(RequestConflict):
            second.checkpoint_correlation("scope", "other objective", correlation_id=None, default_correlation_id="new-run")
    finally:
        second.close()


@pytest.mark.parametrize("mutation", ["binding_sha256='corrupt'", "correlation_id='another-valid-id'"])
def test_checkpoint_binding_corruption_fails_closed(tmp_path, mutation):
    box = SwarmOutbox(tmp_path / "outbox.db")
    try:
        box.checkpoint_correlation("scope", "objective", correlation_id="chosen", default_correlation_id="default")
        box._db.execute("UPDATE checkpoint_submissions SET " + mutation)
        with pytest.raises(InvalidOutboxRecord):
            box.checkpoint_correlation("scope", "objective", correlation_id=None, default_correlation_id="default")
    finally:
        box.close()
