import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.swarm_outbox import SwarmOutbox, SwarmOutboxError
from avrone.swarm_client import DelegationUnknownOutcome, UnknownOutcome, ACTION


class FakeAuthority:
    def __init__(self, fail_once=False):
        self.calls = []
        self.fail_once = fail_once
        self.lock = threading.Lock()

    def delegate(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            if self.fail_once:
                self.fail_once = False
                raise DelegationUnknownOutcome("authority response lost", kwargs)
        return {"capability": {"id": "child-cap", "audience": "spiffe://prod/agent/swarm"}, "signature": "child-signature"}


class FakeSwarm:
    audience = "spiffe://prod/agent/swarm"

    def __init__(self, fail_once=False):
        self.build_calls = 0
        self.send_calls = []
        self.fail_once = fail_once
        self.lock = threading.Lock()

    def build_request(self, **kwargs):
        self.build_calls += 1
        return {
            "envelope": {"message_id": "message-1", "correlation_id": kwargs["correlation_id"]},
            "payload": kwargs["payload"],
            "capability": kwargs["capability"],
            "capability_signature": kwargs["capability_signature"],
        }

    def send_request(self, request):
        with self.lock:
            self.send_calls.append(request)
            if self.fail_once:
                self.fail_once = False
                raise UnknownOutcome("response lost", request)
        return {"job": {"id": "job-1", "status": "queued"}, "receipt": {"signature": "signed"}}


def submit(outbox, authority, swarm, task="test task", correlation="stable-1"):
    return outbox.submit(
        task=task, correlation_id=correlation,
        parent_capability={"id": "parent-cap"}, parent_signature="parent-signature",
        authority_client=authority, swarm_client=swarm,
    )


def test_outbox_persists_before_send_and_returns_queued(tmp_path):
    outbox = SwarmOutbox(tmp_path / "private-outbox")
    authority, swarm = FakeAuthority(), FakeSwarm()
    result = submit(outbox, authority, swarm)
    assert result["status"] == "queued"
    assert len(authority.calls) == 1
    assert len(swarm.send_calls) == 1
    record = next((tmp_path / "private-outbox").glob("*.json"))
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700
    stored = json.loads(record.read_text())
    assert stored["state"] == "queued"
    assert stored["request"]["envelope"]["message_id"] == "message-1"


def test_outbox_retries_exact_signed_envelope_after_unknown_send(tmp_path):
    outbox = SwarmOutbox(tmp_path / "retry-outbox")
    authority, swarm = FakeAuthority(), FakeSwarm(fail_once=True)
    first = submit(outbox, authority, swarm)
    assert first["status"] == "pending"
    assert first["stage"] == "enqueue"
    second = submit(outbox, authority, swarm)
    assert second["status"] == "queued"
    assert len(authority.calls) == 1
    assert swarm.send_calls[0] == swarm.send_calls[1]


def test_outbox_retries_authority_with_same_correlation_after_unknown(tmp_path):
    outbox = SwarmOutbox(tmp_path / "ca-retry-outbox")
    authority, swarm = FakeAuthority(fail_once=True), FakeSwarm()
    first = submit(outbox, authority, swarm)
    assert first["status"] == "pending"
    assert first["stage"] == "delegation"
    second = submit(outbox, authority, swarm)
    assert second["status"] == "queued"
    assert authority.calls[0]["correlation_id"] == authority.calls[1]["correlation_id"] == "stable-1"
    assert len(swarm.send_calls) == 1


def test_outbox_replays_queued_result_without_network(tmp_path):
    outbox = SwarmOutbox(tmp_path / "replay-outbox")
    authority, swarm = FakeAuthority(), FakeSwarm()
    submit(outbox, authority, swarm)
    replay = submit(outbox, authority, swarm)
    assert replay["status"] == "queued"
    assert replay["idempotent_replay"] is True
    assert len(authority.calls) == len(swarm.send_calls) == 1


def test_outbox_rejects_correlation_reuse_for_different_task(tmp_path):
    outbox = SwarmOutbox(tmp_path / "conflict-outbox")
    authority, swarm = FakeAuthority(), FakeSwarm()
    submit(outbox, authority, swarm)
    with pytest.raises(SwarmOutboxError, match="different task"):
        submit(outbox, authority, swarm, task="different task")


def test_outbox_serializes_concurrent_duplicate_submissions(tmp_path):
    outbox = SwarmOutbox(tmp_path / "concurrent-outbox")
    authority, swarm = FakeAuthority(), FakeSwarm()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(outbox, authority, swarm), range(8)))
    assert all(item["status"] == "queued" for item in results)
    assert len(authority.calls) == 1
    assert len(swarm.send_calls) == 1


def test_outbox_rejects_tampered_and_symlinked_records(tmp_path):
    directory = tmp_path / "tamper-outbox"
    outbox = SwarmOutbox(directory)
    authority, swarm = FakeAuthority(), FakeSwarm()
    submit(outbox, authority, swarm)
    record = next(directory.glob("*.json"))
    record.write_text("{bad json")
    with pytest.raises(SwarmOutboxError, match="malformed"):
        submit(outbox, authority, swarm)
    record.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}")
    record.symlink_to(target)
    with pytest.raises(SwarmOutboxError, match="symlink"):
        submit(outbox, authority, swarm)


def test_outbox_rejects_insecure_directory(tmp_path):
    directory = tmp_path / "public-outbox"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(SwarmOutboxError, match="mode 0700"):
        SwarmOutbox(directory)
