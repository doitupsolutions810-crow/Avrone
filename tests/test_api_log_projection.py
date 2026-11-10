"""Regression tests for poisoned historical logs and unsafe event destinations."""
import json
import math
import os
import sys

import pytest
from fastapi.testclient import TestClient

from avrone import server
from avrone.codex import Codex


def event(**updates):
    value = {"run_id": "poison", "kind": "run.start", "agent": "agent",
             "data": {"prompt": "hello"}, "ts": 1.0, "id": "event-1"}
    value.update(updates)
    return value


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), -float("inf")])
def test_api_reads_project_historical_poison_without_rewriting(tmp_path, monkeypatch, bad_number):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "workspace")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as client:
        raw = json.dumps(event(ts=bad_number, data={"prompt": "bad\ud800text",
                         "nested": {"key\udfff": [bad_number, "x\x00y"]}})) + "\n"
        path = server.core().codex.log_dir / "poison.jsonl"
        path.write_text(raw, encoding="utf-8")
        replay = client.get("/api/runs/poison")
        assert replay.status_code == 200
        saved = replay.json()[0]
        assert saved["ts"] is None
        assert saved["data"]["prompt"] == "bad\ufffdtext"
        assert saved["data"]["nested"]["key\ufffd"] == [None, "xy"]
        runs = client.get("/api/runs")
        assert runs.status_code == 200
        assert next(row for row in runs.json() if row["run_id"] == "poison")["started"] is None
        assert path.read_text(encoding="utf-8") == raw


def test_deep_historical_data_has_a_bounded_response_projection(tmp_path):
    codex = Codex(tmp_path)
    nested = "leaf\ud800"
    for _ in range(400):
        nested = [nested]
    raw = json.dumps(event(data={"nested": nested})) + "\n"
    path = codex.log_dir / "deep.jsonl"
    path.write_text(raw, encoding="utf-8")
    replay = codex.replay("deep")
    assert replay
    encoded = json.dumps(replay, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert len(encoded) < 1000
    assert path.read_text(encoding="utf-8") == raw


def test_integer_decoder_guard_skips_only_the_bad_log_record(tmp_path):
    digits = sys.get_int_max_str_digits()
    if not digits:
        pytest.skip("the interpreter integer-string guard is disabled")
    codex = Codex(tmp_path)
    path = codex.log_dir / "number.jsonl"
    path.write_text('{"too_big":' + "9" * (digits + 1) + '}\n' + json.dumps(event()) + "\n")
    assert [entry["id"] for entry in codex.replay("number")] == ["event-1"]
    # A broken first record may omit that run from the listing; it cannot break other runs.
    codex.emit("safe", "run.start", "a", prompt="safe")
    assert [row["run_id"] for row in codex.runs()] == ["safe"]


def test_emit_projects_every_live_event_field_and_cyclic_data(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "off")
    codex = Codex(tmp_path)
    loop = []
    loop.append(loop)
    emitted = codex.emit("live", "think\ud800", "a\udfff", value=math.inf, cycle=loop)
    assert emitted.kind == "think\ufffd" and emitted.agent == "a\ufffd"
    assert emitted.data["value"] is None
    replay = codex.replay("live")
    json.dumps(replay, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert "Infinity" not in (codex.log_dir / "live.jsonl").read_text()


@pytest.mark.parametrize("run_id", ["../escape", "/tmp/absolute-event", "nested/id", "nul\x00id", "bad\ud800id"])
def test_emit_rejects_unsafe_run_id_before_publication(tmp_path, run_id):
    codex = Codex(tmp_path)
    queue = codex.subscribe()
    with pytest.raises(ValueError, match="run id"):
        codex.emit(run_id, "run.start", "a", prompt="never write")
    assert codex.emitted == 0 and queue.empty()
    assert list(codex.log_dir.iterdir()) == []
    assert codex.replay(run_id) == []


def test_symlinked_logs_cannot_escape_through_listing_or_emit(tmp_path):
    codex = Codex(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(event(data={"prompt": "outside-secret"})) + "\n")
    before = outside.read_bytes()
    (codex.log_dir / "linked.jsonl").symlink_to(outside)
    assert codex.replay("linked") == []
    assert codex.runs() == []
    with pytest.raises(ValueError, match="run id"):
        codex.emit("linked", "run.start", "a")
    assert outside.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_nonregular_log_destinations_are_rejected_without_opening(tmp_path):
    codex = Codex(tmp_path)
    os.mkfifo(codex.log_dir / "pipe.jsonl")
    assert codex.runs() == []
    assert codex.replay("pipe") == []
    with pytest.raises(ValueError, match="run id"):
        codex.emit("pipe", "run.start", "a")


@pytest.mark.parametrize("redact", ["on", "off"])
def test_live_event_accepts_oversized_integer_and_unprintable_tool_data(tmp_path, monkeypatch, redact):
    class Unprintable:
        def __str__(self):
            raise RuntimeError("tool object cannot be stringified")

    monkeypatch.setenv("AVRONE_CODEX_REDACT", redact)
    codex = Codex(tmp_path)
    queue = codex.subscribe()
    emitted = codex.emit("odd-tool", "tool.result", "a", giant=10 ** 5000,
                         result={Unprintable(): Unprintable(), "healthy": "preserved"})
    assert queue.get_nowait() is emitted
    assert emitted.data["result"]["healthy"] == "preserved"
    assert isinstance(emitted.data["giant"], str)
    replay = codex.replay("odd-tool")
    assert len(replay) == 1 and replay[0]["data"]["result"]["healthy"] == "preserved"
    json.dumps(replay, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_wide_live_tool_payload_has_a_bounded_event_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "off")
    codex = Codex(tmp_path)
    emitted = codex.emit("wide-tool", "tool.result", "a", rows=[[i, i] for i in range(100_000)])
    assert emitted.data["rows"][0] == [0, 0]
    assert len(emitted.data["rows"]) < 10_000
    assert len((codex.log_dir / "wide-tool.jsonl").read_bytes()) < 500_000
