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
