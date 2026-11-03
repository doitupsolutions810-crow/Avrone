import json
import stat
import threading

import pytest

from avrone.swarm_outbox import (
    SwarmOutbox, SwarmDispatcher, OutboxError, InvalidOutboxRecord, RequestConflict,
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
        box.mark_queued("missing", {"ok": True})
    box.prepare(request())
    result = box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "verified"})
    assert result["state"] == "queued"
    assert box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "verified"})["state"] == "queued"
    with pytest.raises(RequestConflict):
        box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "different"})
    box.close()


def test_rejected_is_terminal_and_cannot_be_queued(tmp_path):
    box = SwarmOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(request())
    box.mark_rejected("msg-1", "forbidden")
    with pytest.raises(OutboxError):
        box.mark_queued("msg-1", {"ok": True})
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
    with pytest.raises(InvalidOutboxRecord): box.mark_queued("msg-1", {"blob": "x" * (128 * 1024)})
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
    for bad in ["", "bad code", "x" * 65, "../bad"]:
        with pytest.raises(ValueError): box.mark_rejected("msg-1", bad)
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
    box.mark_queued("msg-1", {"message_id": "msg-1", "signature": "verified"})
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
    for bad in [None, [], "receipt", 1]:
        with pytest.raises(InvalidOutboxRecord):
            box.mark_queued("msg-1", bad)
    box.close()


def test_independent_dispatchers_share_one_durable_identity(tmp_path):
    barrier = threading.Barrier(2)
    class Client:
        calls = 0
        lock = threading.Lock()
        def send_request(self, req):
            with self.lock:
                self.calls += 1
            barrier.wait(timeout=3)
            return {"job": {"id": "job-1", "status": "queued"},
                    "receipt": {"message_id": "msg-1", "signature": "same-signed-receipt"}}
        def verify_receipt(self, receipt, req):
            assert receipt["message_id"] == req["envelope"]["message_id"]
    boxes = [SwarmOutbox(tmp_path / "outbox.sqlite3") for _ in range(2)]
    client = Client()
    dispatchers = [SwarmDispatcher(client, box) for box in boxes]
    results = []; errors = []
    def submit(dispatcher):
        try:
            results.append(dispatcher.submit_request(request()))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=submit, args=(dispatcher,)) for dispatcher in dispatchers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and len(results) == 2
    assert all(result["state"] == "queued" for result in results)
    assert client.calls == 2
    assert len(boxes[0].pending()) == 0
    assert boxes[0].get("msg-1")["state"] == "queued"
    for box in boxes:
        box.close()
