"""Corrupt-run handling for the persistent JSONL action log."""
import json

from avrone.codex import Codex


def test_runs_skips_malformed_and_non_object_start_events(tmp_path):
    codex = Codex(tmp_path)
    codex.log_dir.joinpath("bad-json.jsonl").write_text("{not json\n")
    codex.log_dir.joinpath("scalar.jsonl").write_text("[]\n")
    codex.log_dir.joinpath("bad-shape.jsonl").write_text(
        json.dumps({"kind": "run.start", "ts": 10, "data": ["wrong shape"]}) + "\n"
    )
    codex.log_dir.joinpath("good.jsonl").write_text(
        json.dumps({"kind": "run.start", "ts": 12, "data": {"prompt": "valid task"}}) + "\n"
    )
    rows = codex.runs(50)
    by_id = {row["run_id"]: row for row in rows}
    assert set(by_id) == {"bad-shape", "good"}
    assert by_id["bad-shape"]["prompt"] == ""
    assert by_id["good"] == {"run_id": "good", "started": 12, "prompt": "valid task"}


def test_replay_skips_corrupt_lines_but_keeps_valid_events(tmp_path):
    codex = Codex(tmp_path)
    p = codex.log_dir / "mixed.jsonl"
    p.write_text("\n".join([
        "{not json",
        "null",
        json.dumps({"kind": "run.start", "data": {"prompt": "x"}}),
        "[]",
        "",
        json.dumps({"kind": "run.done", "data": {}}),
    ]))
    rows = codex.replay("mixed")
    assert [row["kind"] for row in rows] == ["run.start", "run.done"]


def test_replay_refuses_path_escape_and_missing_run(tmp_path):
    codex = Codex(tmp_path)
    assert codex.replay("../outside") == []
    assert codex.replay("missing") == []


def test_negative_run_limit_returns_no_runs(tmp_path):
    codex = Codex(tmp_path)
    codex.emit("r1", "run.start", "Lead", prompt="hello")
    assert codex.runs(-1) == []
