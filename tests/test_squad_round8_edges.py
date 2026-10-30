"""S-mode round 8 regression tests: malformed telemetry, queue poison, tool readiness and skill policy."""
import asyncio
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import jobqueue, telemetry
from avrone import tools_introspect as ti


def test_telemetry_skips_non_event_json_bad_timestamps_and_bad_lines(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    rows = [
        "{broken",
        "[]",
        "null",
        "42",
        json.dumps({"ts": "not-a-timestamp", "kind": "run.start"}),
        json.dumps({"ts": True, "kind": "run.start"}),
        '{"ts":NaN,"kind":"run.start"}',
        json.dumps({"ts": now, "kind": "run.start", "run_id": "good"}),
    ]
    (log_dir / "events.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    got = list(telemetry._iter_events(log_dir, now - 5))
    assert got == [{"ts": now, "kind": "run.start", "run_id": "good"}]


def test_telemetry_skips_timestamp_when_finiteness_check_overflows(tmp_path, monkeypatch):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    (log_dir / "events.jsonl").write_text(
        json.dumps({"ts": int(now), "kind": "run.start", "run_id": "overflow"}) + "\n",
        encoding="utf-8",
    )
    original = telemetry.math.isfinite

    def finite_or_overflow(value):
        if isinstance(value, int):
            raise OverflowError("timestamp cannot be represented as finite float")
        return original(value)

    monkeypatch.setattr(telemetry.math, "isfinite", finite_or_overflow)
    assert list(telemetry._iter_events(log_dir, now - 5)) == []


def test_telemetry_skips_malformed_event_envelopes_without_losing_valid_events(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    rows = [
        {"ts": now, "kind": "tool.call", "agent": "A", "data": []},
        {"ts": now, "kind": "tool.call", "agent": {}, "data": {"tool": "shell"}},
        {"ts": now, "kind": [], "agent": "A", "data": {"tool": "shell"}},
        {"ts": now, "kind": "tool.call", "agent": "A", "run_id": [], "data": {"tool": "shell"}},
        {"ts": now, "kind": "tool.call", "agent": "A", "run_id": "good", "data": {"tool": "shell"}},
    ]
    (log_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    assert list(telemetry._iter_events(log_dir, now - 5)) == [rows[-1]]


def test_telemetry_skips_one_bad_encoding_file_without_losing_good_events(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    (log_dir / "bad.jsonl").write_bytes(b"\xff\xfe\xfa\n")
    (log_dir / "good.jsonl").write_text(
        json.dumps({"ts": now, "kind": "tool.call", "tool": "shell"}) + "\n",
        encoding="utf-8",
    )
    assert list(telemetry._iter_events(log_dir, now - 5)) == [
        {"ts": now, "kind": "tool.call", "tool": "shell"}
    ]


@pytest.mark.parametrize("raw_payload", ["{bad-json", "null", "[]", "42"])
def test_job_queue_quarantines_poison_payload_and_keeps_processing(raw_payload, tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "jobs.db")
        try:
            now = time.time()
            q._db.execute(
                "INSERT INTO jobs(id,kind,payload,status,priority,attempts,max_attempts,created,updated,lease_until) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("poison", "bad", raw_payload, "queued", 1000, 0, 3, now - 10, now - 10, 0),
            )
            good_id = await q.enqueue("good", {"ok": True})
            item = await q.claim()
            assert item == {"id": good_id, "kind": "good", "payload": {"ok": True}, "attempts": 1}
            bad = await q.get("poison")
            assert bad["status"] == "dead"
            assert bad["payload"] is None
            assert "invalid persisted payload" in bad["error"]
            assert (await q.stats()).get("dead") == 1
        finally:
            await q.close()
    asyncio.run(go())


def test_job_queue_validates_payload_shape_and_finite_json_before_insert(tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "jobs.db")
        try:
            with pytest.raises(TypeError, match="payload must be a dict"):
                await q.enqueue("wrong", ["not", "a", "dict"])
            with pytest.raises(ValueError):
                await q.enqueue("nan", {"score": math.nan})
            assert await q.stats() == {}
        finally:
            await q.close()
    asyncio.run(go())


def test_job_queue_large_result_truncation_remains_parseable(tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "jobs.db")
        try:
            jid = await q.enqueue("large", {"ok": True})
            assert (await q.claim())["id"] == jid
            await q.complete(jid, "x" * 150000)
            row = await q.get(jid)
            assert row["status"] == "done"
            assert row["result"]["truncated"] is True
            assert len(row["result"]["preview"]) == 99000
        finally:
            await q.close()
    asyncio.run(go())


def test_runtime_preflight_reports_runner_interface_as_part_of_readiness(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(ti.shutil, "which", lambda name: "/usr/bin/docker")
    ctx = SimpleNamespace(
        core=SimpleNamespace(
            settings=SimpleNamespace(workspace=workspace, provider="local", llm_mode="mock"),
            runner=SimpleNamespace(kind="local", workspace=workspace),
        )
    )
    result = ti.runtime_preflight(ctx)
    assert result["checks"]["runner_can_execute"] is False
    assert result["ready"] is False
    assert "path" not in json.dumps(result).lower()


def test_skills_encode_thermal_resilience_and_bounded_recon_policy():
    root = Path(__file__).resolve().parents[1] / "skills"
    home = (root / "agent-home-resilience" / "SKILL.md").read_text(encoding="utf-8")
    recon = (root / "recon-advanced" / "SKILL.md").read_text(encoding="utf-8")
    assert "report the value as unknown" in home
    assert "verify the pack hash" in home
    assert "timeout is an unknown result" in recon
    assert "Never request host-root" in recon


@pytest.mark.parametrize("requested, expected", [
    (0, 900.0),
    (-1, 900.0),
    (math.inf, 900.0),
    (math.nan, 900.0),
    (True, 900.0),
    (10 ** 12, 7 * 86400.0),
])
def test_job_queue_lease_configuration_is_finite_and_bounded(requested, expected, tmp_path):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "lease.db", lease_s=requested)
        try:
            assert q.lease_s == expected
        finally:
            await q.close()
    asyncio.run(go())


def test_job_queue_reclaims_running_job_at_exact_lease_boundary(tmp_path, monkeypatch):
    async def go():
        clock = [1000.0]
        monkeypatch.setattr(jobqueue.time, "time", lambda: clock[0])
        q = jobqueue.JobQueue(tmp_path / "lease-boundary.db", lease_s=10)
        try:
            jid = await q.enqueue("work", {"ok": True})
            first = await q.claim()
            assert first == {"id": jid, "kind": "work", "payload": {"ok": True}, "attempts": 1}
            clock[0] += 10
            second = await q.claim()
            assert second == {"id": jid, "kind": "work", "payload": {"ok": True}, "attempts": 2}
        finally:
            await q.close()
    asyncio.run(go())


def test_job_queue_two_connections_never_claim_same_job(tmp_path):
    async def go():
        path = tmp_path / "shared.db"
        q1, q2 = jobqueue.JobQueue(path), jobqueue.JobQueue(path)
        try:
            jid = await q1.enqueue("work", {"payload": 1})
            a, b = await asyncio.gather(q1.claim(), q2.claim())
            claimed = [x for x in (a, b) if x is not None]
            assert len(claimed) == 1
            assert claimed[0]["id"] == jid
        finally:
            await q1.close()
            await q2.close()
    asyncio.run(go())


def test_job_queue_retries_then_dead_letters_at_attempt_limit(tmp_path, monkeypatch):
    async def go():
        clock = [1000.0]
        monkeypatch.setattr(jobqueue.time, "time", lambda: clock[0])
        q = jobqueue.JobQueue(tmp_path / "retry.db")
        try:
            jid = await q.enqueue("unstable", {"x": 1}, max_attempts=2)
            assert (await q.claim())["attempts"] == 1
            assert await q.fail(jid, "first") == "requeued"
            clock[0] += 10
            assert (await q.claim())["attempts"] == 2
            assert await q.fail(jid, "second") == "dead"
            assert (await q.get(jid))["status"] == "dead"
            assert await q.claim() is None
        finally:
            await q.close()
    asyncio.run(go())
