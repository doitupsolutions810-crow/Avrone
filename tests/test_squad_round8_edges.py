"""S-mode round 8 regression tests: malformed telemetry, queue poison, tool readiness and skill policy."""
import asyncio
import json
import math
import os
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




def test_telemetry_aggregation_normalizes_untrusted_mode_and_rejects_nonfinite_run_id(tmp_path, monkeypatch):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    rows = [
        {"ts": now, "kind": "run.start", "agent": "A", "run_id": "bad-mode",
         "data": {"mode": ["not", "hashable"]}},
        {"ts": now, "kind": "run.start", "agent": "A", "run_id": "good-mode",
         "data": {"mode": "chat"}},
    ]
    path = log_dir / "events.jsonl"
    raw_bad_id = '{"ts":' + str(now) + ',"kind":"run.start","agent":"A","run_id":NaN,"data":{"mode":"bad-id"}}'
    path.write_text(
        "\n".join([*(json.dumps(row) for row in rows), raw_bad_id]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(telemetry, "node_health", lambda core: {})
    monkeypatch.setattr(telemetry, "fleet_health", lambda core: [])
    core = SimpleNamespace(codex=SimpleNamespace(log_dir=log_dir), active={})

    result = telemetry.telemetry(core, window_s=60)
    assert result["runs"]["total"] == 2
    assert result["runs"]["by_mode"] == {"unknown": 1, "chat": 1}


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


@pytest.mark.parametrize("raw_payload", [
    "{bad-json", "null", "[]", "42",
    '{"score":NaN}', '{"score":Infinity}', '{"score":-Infinity}',
    '{"score":1e10000}', '{"score":-1e10000}',
])
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


@pytest.mark.parametrize("raw_result", ['{"score":NaN}', '{"score":1e10000}'])
def test_job_queue_inspection_rejects_nonfinite_persisted_result(tmp_path, raw_result):
    async def go():
        q = jobqueue.JobQueue(tmp_path / "result-poison.db")
        try:
            jid = await q.enqueue("result", {"ok": True})
            q._db.execute("UPDATE jobs SET result=? WHERE id=?", (raw_result, jid))
            row = await q.get(jid)
            assert row["status"] == "queued"
            assert row["result"] is None
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


def test_telemetry_skips_oversized_line_and_continues_to_next_event(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    good = json.dumps({"ts": now, "kind": "tool.call", "agent": "A",
                       "run_id": "good", "data": {"tool": "shell"}}).encode()
    (log_dir / "events.jsonl").write_bytes(
        b"x" * (telemetry.MAX_TELEMETRY_LINE_BYTES + 4096) + b"\n" + good + b"\n"
    )
    assert list(telemetry._iter_events(log_dir, now - 5)) == [json.loads(good)]


def test_telemetry_reads_recent_tail_of_large_file_within_file_budget(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    good = json.dumps({"ts": now, "kind": "run.start", "agent": "A",
                       "run_id": "tail", "data": {"mode": "chat"}}).encode()
    # The oversized first record begins before the tail window. The iterator must
    # discard its partial suffix and still recover the valid newest event.
    prefix = b"x" * (telemetry.MAX_TELEMETRY_FILE_BYTES + 512_000)
    (log_dir / "large.jsonl").write_bytes(prefix + b"\n" + good + b"\n")
    assert list(telemetry._iter_events(log_dir, now - 5)) == [json.loads(good)]


def test_telemetry_scan_has_a_total_byte_budget(tmp_path, monkeypatch):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    row = json.dumps({"ts": now, "kind": "tool.call", "agent": "A",
                      "run_id": "x", "data": {"tool": "shell"}}).encode() + b"\n"
    monkeypatch.setattr(telemetry, "MAX_TELEMETRY_FILE_BYTES", len(row) * 5)
    monkeypatch.setattr(telemetry, "MAX_TELEMETRY_TOTAL_BYTES", len(row) * 7)
    for i in range(3):
        (log_dir / f"{i:02d}.jsonl").write_bytes(row * 20)
    rows = list(telemetry._iter_events(log_dir, now - 5, max_files=200))
    assert len(rows) <= 7



def test_telemetry_ignores_symlinked_event_files(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    outside = tmp_path / "outside.jsonl"
    now = time.time()
    secret_event = {"ts": now, "kind": "tool.call", "agent": "A",
                    "run_id": "outside", "data": {"tool": "should-not-be-read"}}
    outside.write_text(json.dumps(secret_event) + "\n", encoding="utf-8")
    link = log_dir / "linked.jsonl"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    good = {"ts": now, "kind": "tool.call", "agent": "A",
            "run_id": "inside", "data": {"tool": "shell"}}
    (log_dir / "good.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
    assert list(telemetry._iter_events(log_dir, now - 5)) == [good]



def test_telemetry_skips_deeply_nested_json_and_keeps_following_events(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    deeply_nested = (
        '{"ts":' + str(now) + ',"kind":"tool.call","agent":"A","run_id":"nested","data":{"x":'
        + "[" * 1200 + "0" + "]" * 1200 + "}}"
    )
    valid = {"ts": now, "kind": "tool.call", "agent": "A",
             "run_id": "valid-after-nesting", "data": {"tool": "shell"}}
    (log_dir / "events.jsonl").write_text(
        deeply_nested + "\n" + json.dumps(valid) + "\n", encoding="utf-8"
    )
    assert list(telemetry._iter_events(log_dir, now - 5)) == [valid]


def test_telemetry_skips_overlarge_integer_run_id_and_keeps_following_events(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    overlarge_id = '{"ts":' + str(now) + ',"kind":"run.start","agent":"A","run_id":' + ("9" * 5000) + ',"data":{"mode":"bad"}}'
    valid = {"ts": now, "kind": "run.start", "agent": "A",
             "run_id": "valid-after-large-id", "data": {"mode": "chat"}}
    (log_dir / "events.jsonl").write_text(
        overlarge_id + "\n" + json.dumps(valid) + "\n", encoding="utf-8"
    )
    assert list(telemetry._iter_events(log_dir, now - 5)) == [valid]


def test_telemetry_rejects_unreasonably_large_integer_run_id(tmp_path):
    log_dir = tmp_path / "events"
    log_dir.mkdir()
    now = time.time()
    too_large = 1 << 100
    valid = {"ts": now, "kind": "run.start", "agent": "A",
             "run_id": too_large, "data": {"mode": "chat"}}
    (log_dir / "events.jsonl").write_text(json.dumps(valid) + "\n", encoding="utf-8")
    assert list(telemetry._iter_events(log_dir, now - 5)) == []
