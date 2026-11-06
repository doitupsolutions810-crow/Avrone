import json
import hashlib
import stat
import threading

import pytest

from avrone.swarm_outbox import (
    SwarmOutbox, SwarmDispatcher, OutboxError, InvalidOutboxRecord, RequestConflict, DispatchLeaseLost,
)


def request(mid="msg-1", cid="corr-1", objective="do work"):
    return {
        "envelope": {"message_id": mid, "correlation_id": cid, "signature": "signed"},
        "payload": {"objective": objective},
        "capability": {"id": "cap-1"}, "capability_signature": "ca-signed",
    }


def test_prepare_persists_mode_0600_and_round_trips(tmp_path):
    box = SwarmOutbox(tmp_path / "secure" / "outbox.sqlite3")
    saved = box.prepare(request())
    assert saved["state"] == "pending"
    assert saved["request"] == request()
    assert stat.S_IMODE((tmp_path / "secure" / "outbox.sqlite3").stat().st_mode) == 0o600
    box.close()


def test_pending_survives_process_restart(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    first = SwarmOutbox(path)
    first.prepare(request())
    first.close()
    second = SwarmOutbox(path)
    assert second.pending()[0]["request"] == request()
    second.close()


def test_identical_prepare_is_idempotent(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    one = box.prepare(request())
    two = box.prepare(request())
    assert one["request_sha256"] == two["request_sha256"]
    assert len(box.pending()) == 1
    box.close()


def test_message_id_collision_with_different_bytes_is_rejected(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    with pytest.raises(RequestConflict):
        box.prepare(request(objective="different"))
    box.close()


def test_mark_queued_requires_existing_pending_and_is_idempotent(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(KeyError):
        box.mark_queued("missing", {"ok": True}, owner_token="0" * 48)
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    result = box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "verified"}, owner_token=owner)
    assert result["state"] == "queued"
    assert box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "verified"}, owner_token=owner)["state"] == "queued"
    with pytest.raises(RequestConflict):
        box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "different"}, owner_token=owner)
    box.close()


def test_rejected_is_terminal_and_cannot_be_queued(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    box.mark_rejected("msg-1", "forbidden", owner_token=owner)
    with pytest.raises(OutboxError):
        box.mark_queued("msg-1", {"ok": True}, owner_token=owner)
    assert box.pending() == []
    box.close()


@pytest.mark.parametrize("bad", [None, [], "x", {"envelope": {}},
    {"envelope": {"message_id": "../x", "correlation_id": "c"}, "payload": {}, "capability": {}, "capability_signature": "x"},
    {"envelope": {"message_id": "m", "correlation_id": ""}, "payload": {}, "capability": {}, "capability_signature": "x"},
])
def test_rejects_malformed_request_identity(tmp_path, bad):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(InvalidOutboxRecord):
        box.prepare(bad)
    box.close()


def test_rejects_nan_and_surrogate_payload(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    bad = request(); bad["payload"]["x"] = float("nan")
    with pytest.raises(InvalidOutboxRecord): box.prepare(bad)
    bad = request(); bad["payload"]["x"] = "\ud800"
    with pytest.raises(InvalidOutboxRecord): box.prepare(bad)
    box.close()


def test_request_and_receipt_size_limits(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    huge = request(); huge["payload"]["blob"] = "x" * (512 * 1024)
    with pytest.raises(InvalidOutboxRecord): box.prepare(huge)
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    with pytest.raises(InvalidOutboxRecord):
        box.mark_queued("msg-1", {"blob": "x" * (128 * 1024)}, owner_token=owner)
    box.close()


def test_database_symlink_is_rejected(tmp_path):
    target = tmp_path / "target"; target.write_text("not sqlite")
    link = tmp_path / "outbox.sqlite3"; link.symlink_to(target)
    with pytest.raises(OSError): SwarmOutbox(link)


def test_pending_limit_validation(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    for bad in [True, 0, -1, 1001, 1.5, "10"]:
        with pytest.raises(ValueError): box.pending(bad)
    box.close()


def test_concurrent_identical_prepare_has_one_record(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    boxes = [SwarmOutbox(path) for _ in range(6)]
    errors = []
    def worker(box):
        try: box.prepare(request())
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=worker, args=(b,)) for b in boxes]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    assert len(boxes[0].pending()) == 1
    for b in boxes: b.close()


def test_concurrent_conflicting_prepare_has_one_winner(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    boxes = [SwarmOutbox(path) for _ in range(2)]
    outcomes = []
    def worker(box, text):
        try: box.prepare(request(objective=text)); outcomes.append("ok")
        except RequestConflict: outcomes.append("conflict")
    threads = [threading.Thread(target=worker, args=(boxes[0], "A")), threading.Thread(target=worker, args=(boxes[1], "B"))]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(outcomes) == ["conflict", "ok"]
    for b in boxes: b.close()


def test_corrupt_stored_blob_is_detected(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    box._db.execute("UPDATE swarm_outbox SET request_json=? WHERE message_id='msg-1'", (b'{"bad":true}',))
    with pytest.raises(InvalidOutboxRecord): box.get("msg-1")
    box.close()


def test_mark_rejected_rejects_bad_codes(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    for bad in ["", "bad code", "x" * 65, "../bad"]:
        with pytest.raises(ValueError):
            box.mark_rejected("msg-1", bad, owner_token=owner)
    box.close()


def test_dispatch_persists_before_send_and_reports_only_queued(tmp_path):
    class Client:
        def __init__(self, box): self.box = box; self.observed = None
        def send_request(self, req):
            self.observed = self.box.get(req["envelope"]["message_id"])
            return {"job": {"id": "job-1", "status": "queued"}, "receipt": {"message_id": "msg-1", "signature": "sig"}}
        def verify_receipt(self, receipt, req): assert receipt["message_id"] == req["envelope"]["message_id"]
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    client = Client(box)
    result = SwarmDispatcher(client, box).submit_request(request())
    assert client.observed["state"] == "pending"
    assert result["state"] == "queued"
    assert "completed" not in result
    assert box.get("msg-1")["state"] == "queued"
    box.close()


def test_dispatch_unknown_outcome_keeps_exact_request_for_retry(tmp_path):
    from avrone.swarm_client import UnknownOutcome
    class Client:
        def __init__(self): self.calls = []
        def send_request(self, req):
            self.calls.append(json.dumps(req, separators=(",", ":")))
            if len(self.calls) == 1: raise UnknownOutcome("timeout", req)
            return {"job": {"id": "job-1", "status": "queued"}, "receipt": {"message_id": "msg-1", "signature": "sig"}}
        def verify_receipt(self, receipt, req): assert receipt["message_id"] == req["envelope"]["message_id"]
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    client = Client(); dispatcher = SwarmDispatcher(client, box)
    first = dispatcher.submit_request(request())
    assert first["state"] == "unknown" and first["retryable"] is True
    second = dispatcher.retry("msg-1")
    assert second["state"] == "queued"
    assert client.calls[0] == client.calls[1]
    box.close()


def test_dispatch_rejected_is_terminal_and_never_falls_back(tmp_path):
    from avrone.swarm_client import SwarmRejected
    class Client:
        def send_request(self, req): raise SwarmRejected(403, "denied")
        def verify_receipt(self, receipt, req): raise AssertionError("must not verify")
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    result = SwarmDispatcher(Client(), box).submit_request(request())
    assert result == {"state": "rejected", "message_id": "msg-1", "error_code": "http_403"}
    assert box.get("msg-1")["state"] == "rejected"
    box.close()


def test_dispatch_does_not_persist_unverified_receipt(tmp_path):
    class Client:
        def send_request(self, req): return {"job": {"id": "job-1"}, "receipt": {"bad": True}}
        def verify_receipt(self, receipt, req): raise ValueError("invalid signature")
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(ValueError): SwarmDispatcher(Client(), box).submit_request(request())
    assert box.get("msg-1")["state"] == "pending"
    box.close()


def test_retry_returns_stored_queue_receipt_without_resending(tmp_path):
    class Client:
        calls = 0
        def send_request(self, req):
            self.calls += 1
            return {"job": {"id": "job-1"}, "receipt": {"message_id": "msg-1", "signature": "sig"}}
        def verify_receipt(self, receipt, req): pass
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    client = Client(); dispatcher = SwarmDispatcher(client, box)
    dispatcher.submit_request(request())
    result = dispatcher.retry("msg-1")
    assert result["state"] == "queued" and client.calls == 1
    box.close()


def test_corrupt_stored_receipt_is_detected(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "verified"}, owner_token=owner)
    box._db.execute("UPDATE swarm_outbox SET receipt_json=? WHERE message_id='msg-1'", (b'{"tampered":true}',))
    with pytest.raises(InvalidOutboxRecord, match="receipt digest"):
        box.get("msg-1")
    box.close()


def test_request_shape_requires_payload_capability_and_signature(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    for field, value in [("payload", []), ("capability", []), ("capability_signature", ""), ("envelope", {"message_id":"m","correlation_id":"c"})]:
        bad = request(); bad[field] = value
        with pytest.raises(InvalidOutboxRecord):
            box.prepare(bad)
    box.close()


def test_remote_completed_state_never_becomes_local_task_completion(tmp_path):
    class Client:
        def send_request(self, req):
            return {"job": {"id": "job-1", "status": "completed"},
                    "receipt": {"message_id": "msg-1", "signature": "verified"}}
        def verify_receipt(self, receipt, req): pass
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    result = SwarmDispatcher(Client(), box).submit_request(request())
    assert result["state"] == "queued"
    assert "completed" not in result
    box.close()


def test_rejected_retry_is_terminal_and_does_not_resend(tmp_path):
    from avrone.swarm_client import SwarmRejected
    class Client:
        calls = 0
        def send_request(self, req):
            self.calls += 1
            raise SwarmRejected(403, "denied")
        def verify_receipt(self, receipt, req): pass
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    client = Client(); dispatcher = SwarmDispatcher(client, box)
    assert dispatcher.submit_request(request())["state"] == "rejected"
    assert dispatcher.retry("msg-1")["state"] == "rejected"
    assert client.calls == 1
    box.close()


def test_get_rejects_invalid_message_id(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    for bad in [None, "", "../secret", "x" * 129, 3]:
        with pytest.raises(InvalidOutboxRecord):
            box.get(bad)
    box.close()


def test_mark_queued_requires_receipt_object(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    for bad in [None, [], "receipt", 1]:
        with pytest.raises(InvalidOutboxRecord):
            box.mark_queued("msg-1", bad, owner_token=owner)
    box.close()


def test_concurrent_duplicate_dispatches_send_only_once(tmp_path):
    class Client:
        calls = 0
        lock = threading.Lock()
        def send_request(self, req):
            with self.lock:
                self.calls += 1
            return {"job": {"id": "job-1", "status": "queued"},
                    "receipt": {"message_id": "msg-1", "signature": "same-signed-receipt"}}
        def verify_receipt(self, receipt, req):
            assert receipt["message_id"] == req["envelope"]["message_id"]
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    client = Client(); dispatcher = SwarmDispatcher(client, box)
    results = []; errors = []
    def submit():
        try:
            results.append(dispatcher.submit_request(request()))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and len(results) == 2
    assert all(result["state"] in {"queued", "unknown"} for result in results)
    assert any(result["state"] == "queued" for result in results)
    assert client.calls == 1
    assert len(box.pending()) == 0
    assert box.get("msg-1")["state"] == "queued"
    box.close()


def test_cross_connection_stale_owner_cannot_commit_queue_result(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    original = SwarmOutbox(path)
    successor_box = SwarmOutbox(path)
    original.prepare(request())
    stale_owner = original.claim_dispatch("msg-1", lease_seconds=30)

    successor_box._db.execute(
        "UPDATE dispatch_leases SET lease_until=0 WHERE message_id='msg-1'"
    )
    current_owner = successor_box.claim_dispatch("msg-1", lease_seconds=30)
    assert current_owner and current_owner != stale_owner

    receipt = {"message_id": "msg-1", "signature": "verified"}
    with pytest.raises(DispatchLeaseLost):
        original.mark_queued("msg-1", receipt, owner_token=stale_owner)
    assert original.get("msg-1")["state"] == "pending"

    assert successor_box.mark_queued(
        "msg-1", receipt, owner_token=current_owner
    )["state"] == "queued"
    original.close()
    successor_box.close()


def test_cross_connection_stale_owner_cannot_commit_rejection(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    original = SwarmOutbox(path)
    successor_box = SwarmOutbox(path)
    original.prepare(request())
    stale_owner = original.claim_dispatch("msg-1", lease_seconds=30)

    successor_box._db.execute(
        "UPDATE dispatch_leases SET lease_until=0 WHERE message_id='msg-1'"
    )
    current_owner = successor_box.claim_dispatch("msg-1", lease_seconds=30)
    assert current_owner and current_owner != stale_owner

    with pytest.raises(DispatchLeaseLost):
        original.mark_rejected("msg-1", "http_403", owner_token=stale_owner)
    assert original.get("msg-1")["state"] == "pending"

    assert successor_box.mark_rejected(
        "msg-1", "http_403", owner_token=current_owner
    )["state"] == "rejected"
    original.close()
    successor_box.close()


def test_expired_dispatch_owner_cannot_commit_a_queue_result(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    stale_owner = box.claim_dispatch("msg-1", lease_seconds=30)
    box._db.execute("UPDATE dispatch_leases SET lease_until=0 WHERE message_id='msg-1'")
    current_owner = box.claim_dispatch("msg-1", lease_seconds=30)
    assert current_owner and current_owner != stale_owner

    receipt = {"message_id": "msg-1", "signature": "verified"}
    with pytest.raises(DispatchLeaseLost):
        box.mark_queued("msg-1", receipt, owner_token=stale_owner)
    assert box.get("msg-1")["state"] == "pending"

    assert box.mark_queued("msg-1", receipt, owner_token=current_owner)["state"] == "queued"
    box.close()


def test_expired_dispatch_owner_cannot_commit_a_rejection(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    stale_owner = box.claim_dispatch("msg-1", lease_seconds=30)
    box._db.execute("UPDATE dispatch_leases SET lease_until=0 WHERE message_id='msg-1'")
    current_owner = box.claim_dispatch("msg-1", lease_seconds=30)
    assert current_owner and current_owner != stale_owner

    with pytest.raises(DispatchLeaseLost):
        box.mark_rejected("msg-1", "http_403", owner_token=stale_owner)
    assert box.get("msg-1")["state"] == "pending"

    assert box.mark_rejected("msg-1", "http_403", owner_token=current_owner)["state"] == "rejected"
    box.close()


def test_dispatcher_reports_unknown_when_send_returns_after_lease_recovery(tmp_path):
    class Client:
        def __init__(self, outbox):
            self.outbox = outbox
            self.successor = None

        def send_request(self, req):
            self.outbox._db.execute(
                "UPDATE dispatch_leases SET lease_until=0 WHERE message_id='msg-1'"
            )
            self.successor = self.outbox.claim_dispatch("msg-1", lease_seconds=30)
            return {"job": {"id": "job-1", "status": "queued"},
                    "receipt": {"message_id": "msg-1", "signature": "verified"}}

        def verify_receipt(self, receipt, req):
            assert receipt["message_id"] == req["envelope"]["message_id"]

    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    client = Client(box)
    result = SwarmDispatcher(client, box).submit_request(request())

    assert result["state"] == "unknown"
    assert "lease expired" in result["reason"]
    assert box.get("msg-1")["state"] == "pending"
    assert client.successor
    assert box.release_dispatch("msg-1", client.successor)
    box.close()


def test_dispatch_lease_blocks_concurrent_sender_and_recovers_after_expiry(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    first = box.claim_dispatch("msg-1", lease_seconds=30)
    assert isinstance(first, str) and len(first) == 48
    assert box.claim_dispatch("msg-1", lease_seconds=30) is None
    box._db.execute("UPDATE dispatch_leases SET lease_until=0 WHERE message_id='msg-1'")
    second = box.claim_dispatch("msg-1", lease_seconds=30)
    assert isinstance(second, str) and second != first
    assert box.release_dispatch("msg-1", first) is False
    assert box.release_dispatch("msg-1", second) is True
    box.close()


@pytest.mark.parametrize("bad", [True, 0, -1, 601, 1.5, "30"])
def test_dispatch_lease_duration_is_bounded(tmp_path, bad):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    with pytest.raises(ValueError):
        box.claim_dispatch("msg-1", bad)
    box.close()


def test_dispatcher_does_not_send_when_another_worker_owns_lease(tmp_path):
    class Client:
        calls = 0
        def send_request(self, req):
            self.calls += 1
            raise AssertionError("a second worker must not send while the lease is active")
        def verify_receipt(self, receipt, req):
            raise AssertionError("no receipt expected")
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    owner = box.claim_dispatch("msg-1")
    client = Client()
    result = SwarmDispatcher(client, box).retry("msg-1")
    assert result["state"] == "unknown"
    assert result["reason"] == "dispatch is already in progress"
    assert client.calls == 0
    assert box.release_dispatch("msg-1", owner)
    box.close()


def test_get_by_correlation_returns_exact_persisted_request(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    original = request()
    box.prepare(original)
    found = box.get_by_correlation("corr-1")
    assert found["request"] == original
    assert found["state"] == "pending"
    box.close()


def test_get_by_correlation_rejects_invalid_input(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    for bad in [None, "", "../bad", "x" * 129]:
        with pytest.raises(InvalidOutboxRecord):
            box.get_by_correlation(bad)
    box.close()


def test_get_by_correlation_returns_none_when_not_found(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    assert box.get_by_correlation("not-yet-submitted") is None
    box.close()


def authority_request(cid="ca-corr-1", jti="stable-jti", parent="parent-1"):
    return {
        "correlation_id": cid,
        "parent_capability": {"id": parent},
        "parent_signature": "parent-signature",
        "audience": "spiffe://prod/agent/swarm",
        "rights": [{"type": "Execute", "resource": "swarm.jobs", "method": "POST"}],
        "ttl_seconds": 120,
        "requester_assertion": {
            "schema": "capability-authority.delegation-assertion/v1",
            "issuer": "spiffe://prod/agent/avrone",
            "subject": "spiffe://prod/agent/avrone",
            "audience": "spiffe://prod/broker",
            "jti": jti,
            "issued_at": "2026-11-03T09:00:00.000Z",
            "expires_at": "2026-11-03T09:00:30.000Z",
            "signature": "signed-assertion",
        },
    }


def test_authority_request_exact_bytes_survive_restart(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    first = SwarmOutbox(path)
    saved = first.prepare_authority_request("ca-corr-1", authority_request())
    digest = saved["request_sha256"]
    first.close()
    second = SwarmOutbox(path)
    recovered = second.get_authority_request("ca-corr-1")
    assert recovered["request"] == authority_request()
    assert recovered["request_sha256"] == digest
    assert second.prepare_authority_request("ca-corr-1", authority_request())["request_sha256"] == digest
    second.close()


def test_authority_correlation_cannot_be_rebound_to_new_assertion_or_parent(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare_authority_request("ca-corr-1", authority_request())
    for changed in [authority_request(jti="different-jti"), authority_request(parent="other-parent")]:
        with pytest.raises(RequestConflict):
            box.prepare_authority_request("ca-corr-1", changed)
    box.close()


def test_authority_request_rejects_invalid_correlation_and_unsigned_assertion(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(InvalidOutboxRecord):
        box.prepare_authority_request("../bad", authority_request("../bad"))
    unsigned = authority_request()
    unsigned["requester_assertion"]["signature"] = ""
    with pytest.raises(InvalidOutboxRecord):
        box.prepare_authority_request("ca-corr-1", unsigned)
    box.close()


def test_authority_request_detects_database_tampering(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare_authority_request("ca-corr-1", authority_request())
    with box._lock:
        box._db.execute("UPDATE authority_delegations SET request_json=? WHERE correlation_id=?",
                        (b'{"correlation_id":"ca-corr-1"}', "ca-corr-1"))
    with pytest.raises(InvalidOutboxRecord, match="digest mismatch"):
        box.get_authority_request("ca-corr-1")
    box.close()


def test_concurrent_authority_prepare_is_idempotent(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    results, errors = [], []
    def prepare():
        try:
            results.append(box.prepare_authority_request("ca-corr-1", authority_request()))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=prepare) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert not errors and len(results) == 8
    assert len({item["request_sha256"] for item in results}) == 1
    assert box.get_authority_request("ca-corr-1")["request"] == authority_request()
    box.close()


def test_authority_request_rejects_non_finite_json_and_oversize(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    bad = authority_request()
    bad["ttl_seconds"] = float("nan")
    with pytest.raises(InvalidOutboxRecord, match="finite UTF-8 JSON"):
        box.prepare_authority_request("ca-corr-1", bad)
    huge = authority_request()
    huge["payload_padding"] = "x" * (256 * 1024)
    with pytest.raises(InvalidOutboxRecord, match="size limit"):
        box.prepare_authority_request("ca-corr-1", huge)
    box.close()


def test_authority_request_rejects_duplicate_json_keys_even_with_matching_digest(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare_authority_request("ca-corr-1", authority_request())
    raw = b'{"correlation_id":"ca-corr-1","correlation_id":"attacker"}'
    digest = hashlib.sha256(raw).hexdigest()
    with box._lock:
        box._db.execute("UPDATE authority_delegations SET request_json=?,request_sha256=? WHERE correlation_id=?",
                        (raw, digest, "ca-corr-1"))
    with pytest.raises(InvalidOutboxRecord, match="duplicate JSON key"):
        box.get_authority_request("ca-corr-1")
    box.close()


def test_authority_request_rejects_rehashed_nonfinite_and_oversized_records(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare_authority_request("ca-corr-1", authority_request())

    malformed_records = [
        b'{"correlation_id":"ca-corr-1","requester_assertion":{"signature":"sig"},"value":NaN}',
        (b'{"correlation_id":"ca-corr-1","requester_assertion":{"signature":"sig"},"padding":"'
         + b"x" * (256 * 1024) + b'"}'),
    ]
    for raw in malformed_records:
        digest = hashlib.sha256(raw).hexdigest()
        with box._lock:
            box._db.execute(
                "UPDATE authority_delegations SET request_json=?,request_sha256=? WHERE correlation_id=?",
                (raw, digest, "ca-corr-1"),
            )
        with pytest.raises(InvalidOutboxRecord):
            box.get_authority_request("ca-corr-1")

    box.close()


def test_authority_request_invalid_correlation_type_fails_closed(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(InvalidOutboxRecord):
        box.get_authority_request(None)
    with pytest.raises(InvalidOutboxRecord):
        box.prepare_authority_request(7, authority_request())
    box.close()


def test_dispatch_lease_is_shared_across_independent_sqlite_connections(tmp_path):
    import threading

    path = tmp_path / "outbox.sqlite3"
    first_box = SwarmOutbox(path)
    second_box = SwarmOutbox(path)
    first_box.prepare(request())
    entered = threading.Event()
    release = threading.Event()

    class Client:
        calls = 0
        lock = threading.Lock()
        def send_request(self, req):
            with self.lock:
                self.calls += 1
            entered.set()
            assert release.wait(timeout=3)
            return {"job": {"id": "job-1", "status": "queued"},
                    "receipt": {"message_id": "msg-1", "signature": "verified"}}
        def verify_receipt(self, receipt, req):
            assert receipt["message_id"] == req["envelope"]["message_id"]

    client = Client()
    first = SwarmDispatcher(client, first_box)
    second = SwarmDispatcher(client, second_box)
    results = []
    thread = threading.Thread(target=lambda: results.append(first.submit_request(request())))
    thread.start()
    assert entered.wait(timeout=3)
    competing = second.retry("msg-1")
    assert competing["state"] == "unknown"
    assert competing["reason"] == "dispatch is already in progress"
    assert client.calls == 1
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert results[0]["state"] == "queued"
    assert second.retry("msg-1")["state"] == "queued"
    assert client.calls == 1
    first_box.close()
    second_box.close()
