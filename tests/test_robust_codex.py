"""Robustness: corrupt or truncated JSONL run logs must be skipped, never crash runs()/replay()."""
import json
import threading

from avrone.codex import Codex


def _good(run_id, kind, i=0):
    return json.dumps({"run_id": run_id, "kind": kind, "agent": "T", "data": {"prompt": f"p{i}"},
                       "ts": 1.0 + i, "id": f"id{i}"})


def test_replay_skips_truncated_last_line(tmp_path):
    c = Codex(tmp_path)
    c.emit("r1", "run.start", "T", prompt="hi")
    c.emit("r1", "agent.spawn", "A")
    # a crash mid-write leaves a half line at the end of the log
    with (c.log_dir / "r1.jsonl").open("a") as f:
        f.write('{"run_id": "r1", "kind": "agent.th')
    evs = c.replay("r1")
    assert [e["kind"] for e in evs] == ["run.start", "agent.spawn"]


def test_replay_skips_garbage_and_non_object_lines(tmp_path):
    c = Codex(tmp_path)
    p = c.log_dir / "r2.jsonl"
    p.write_bytes(("\n".join([_good("r2", "run.start"), "not json", "[1,2]", "42", "null",
                              _good("r2", "run.done", 1)]) + "\n").encode()
                  + b"\xff\xfe\x00broken bytes\n")
    assert [e["kind"] for e in c.replay("r2")] == ["run.start", "run.done"]


def test_replay_invalid_utf8_does_not_raise(tmp_path):
    c = Codex(tmp_path)
    (c.log_dir / "r3.jsonl").write_bytes(b"\xc3\x28\xa0\xa1\n" + _good("r3", "run.start").encode() + b"\n")
    assert [e["kind"] for e in c.replay("r3")] == ["run.start"]


def test_runs_tolerates_corrupt_first_line_and_bad_bytes(tmp_path):
    c = Codex(tmp_path)
    c.emit("ok", "run.start", "T", prompt="hello")
    (c.log_dir / "bad.jsonl").write_bytes(b"\xff\xfe garbage\n")
    (c.log_dir / "list.jsonl").write_text("[1, 2]\n")
    (c.log_dir / "empty.jsonl").write_text("")
    (c.log_dir / "nodata.jsonl").write_text(json.dumps({"ts": 5, "data": "oops"}) + "\n")
    runs = {r["run_id"]: r for r in c.runs()}
    assert runs["ok"]["prompt"] == "hello"
    assert "bad" not in runs and "list" not in runs and "empty" not in runs
    assert runs["nodata"]["prompt"] == ""


def test_runs_survives_file_deleted_between_glob_and_stat(tmp_path, monkeypatch):
    c = Codex(tmp_path)
    c.emit("a", "run.start", "T", prompt="x")
    ghost = c.log_dir / "ghost.jsonl"
    real_glob = type(c.log_dir).glob

    def glob(self, pat):
        yield from real_glob(self, pat)
        yield ghost  # listed, but already gone

    monkeypatch.setattr(type(c.log_dir), "glob", glob)
    assert [r["run_id"] for r in c.runs()] == ["a"]


def test_concurrent_emit_from_threads_keeps_every_line_whole(tmp_path):
    c = Codex(tmp_path)
    big = "x" * 20000  # larger than a pipe/page buffer, so unlocked writes can interleave
    n_threads, per = 8, 25

    def work(t):
        for i in range(per):
            c.emit("rc", "agent.think", f"T{t}", text=big, i=i)

    ts = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lines = (c.log_dir / "rc.jsonl").read_text().splitlines()
    assert len(lines) == n_threads * per
    assert all(json.loads(l)["data"]["text"] == big for l in lines)
    assert len(c.replay("rc")) == n_threads * per


def test_replay_rejects_path_traversal(tmp_path):
    c = Codex(tmp_path)
    (tmp_path / "secret.jsonl").write_text(_good("x", "run.start") + "\n")
    assert c.replay("../secret") == []


def test_api_replay_and_runs_survive_truncated_log(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as client:
        rid = client.post("/api/chat", json={"message": "hi", "mode": "chat"}).json()["run_id"]
        log = server.core().codex.log_dir / f"{rid}.jsonl"
        with log.open("ab") as f:
            f.write(b'{"run_id": "' + rid.encode() + b'", "kind": "agent.th\xff')
        r = client.get(f"/api/runs/{rid}")
        assert r.status_code == 200
        kinds = [e["kind"] for e in r.json()]
        assert kinds[0] == "run.start" and kinds[-1] == "run.done"
        assert client.get("/api/runs").status_code == 200
