"""Offline tests: server-managed checkpoints over the API (POST /api/chat checkpoint_id,
GET/DELETE /api/checkpoints), plus the id sanitiser and store helpers. Mock LLM, no network."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from avrone import checkpoint as ckm
from avrone.checkpoint import Checkpoint

ADMIN = {"x-admin-token": "test-token"}
PLAN2 = json.dumps({"subtasks": [{"role": "A", "focus": "alpha"}, {"role": "B", "focus": "beta"}]})


class CountingLLM:
    """Scripted model counting calls by kind; `hang` makes workers on that focus block forever."""

    def __init__(self, hang=None):
        self.hang = hang
        self.calls = {"plan": 0, "worker": 0, "lead": 0, "deeplead": 0, "reviewer": 0}

    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the PLANNER" in system:
            self.calls["plan"] += 1
            return PLAN2
        if "You are the DEEP LEAD" in system:
            self.calls["deeplead"] += 1
            return ("## Verdict\nOK. Confidence: High\n\n## Corrections\n- none\n\n## Additions\n- x\n\n"
                    "## Counterpoints\n- y\n\n## Sources\n- https://example.com\n\n## Bottom line\nfine")
        if "You are the LEAD" in system:
            self.calls["lead"] += 1
            return "LEAD MERGED " + "x" * 400
        if self.hang and f"Your focus: {self.hang}" in system:
            await asyncio.sleep(3600)
        if "DEEP ANALYSIS" in system:
            self.calls["reviewer"] += 1
            return '{"final": "review ok"}'
        self.calls["worker"] += 1
        return '{"final": "worker ok"}'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(server.settings, "max_steps", 2)
    with TestClient(server.app) as c:
        llm = CountingLLM()
        server.state["core"].llm = llm
        c.llm = llm
        c.core = server.state["core"]
        c.ckdir = tmp_path / "data" / "checkpoints"
        yield c


def _post(c, **body):
    return c.post("/api/chat", json={"message": "plan a trip", "mode": "squad", "squad_size": 2, **body})


# ---------------------------------------------------------------- sanitiser / helpers
@pytest.mark.parametrize("bad", ["", "a" * 65, "../x", "a/b", "a.b", "a b", "é", None, 5, ["x"]])
def test_sanitize_rejects_bad_ids(bad):
    with pytest.raises(ckm.CheckpointError):
        ckm.sanitize_id(bad)


@pytest.mark.parametrize("good", ["a", "A-b_9", "x" * 64])
def test_sanitize_accepts_good_ids(good):
    assert ckm.sanitize_id(good) == good


def test_client_ids_cannot_use_fleet_prefix():
    with pytest.raises(ckm.CheckpointError):
        ckm.sanitize_client_id("fleet-hub-x")


def test_managed_path_stays_in_checkpoints_dir(tmp_path):
    p = ckm.managed_path(tmp_path, "job1")
    assert p == tmp_path / "checkpoints" / "job1.json"
    for bad in ("..", "../../etc/passwd", "a/b", ""):
        with pytest.raises(ckm.CheckpointError):
            ckm.managed_path(tmp_path, bad)


def test_fleet_key_and_bad_sender():
    assert ckm.fleet_key("edge-1", "job") == "fleet-edge-1-job"
    for sender in ("", "Bad", "a/b", None):
        with pytest.raises(ckm.CheckpointError):
            ckm.fleet_key(sender, "job")


def test_check_compatible_and_owner(tmp_path):
    p = ckm.managed_path(tmp_path, "k")
    assert ckm.check_compatible(p, "t", "squad") is None  # missing file: fresh
    p.parent.mkdir(parents=True)
    p.write_text("not json")
    assert ckm.check_compatible(p, "t", "squad") is None  # corrupt: fresh (trinity treats it as empty)
    p.unlink()
    Checkpoint(p).begin("t", "squad", "r1", n=2)
    assert ckm.check_compatible(p, "t", "squad")["run_id"] == "r1"
    with pytest.raises(ckm.CheckpointConflict, match="different squad task"):
        ckm.check_compatible(p, "other", "squad")
    with pytest.raises(ckm.CheckpointConflict):
        ckm.check_compatible(p, "t", "deep")
    with pytest.raises(ckm.CheckpointConflict, match="another owner"):
        ckm.check_compatible(p, "t", "squad", owner="fleet:x")
    ckm.stamp_owner(p, "fleet:x")
    ckm.stamp_owner(p, "fleet:x")  # idempotent
    assert ckm.check_compatible(p, "t", "squad", owner="fleet:x")["run_id"] == "r1"
    with pytest.raises(ckm.CheckpointConflict):
        ckm.check_compatible(p, "t", "squad")  # an owned file is not reachable without that owner
    assert Checkpoint(p).get("meta")["task"] == "t"  # the owner stamp does not disturb the steps


def test_claim_is_exclusive_and_released(tmp_path):
    p = ckm.managed_path(tmp_path, "k")
    with ckm.claimed(p):
        assert ckm.is_busy(p)
        with pytest.raises(ckm.CheckpointConflict, match="in use"):
            ckm.claim(p)
        with pytest.raises(ckm.CheckpointConflict, match="in use"):
            ckm.check_compatible(p, "t", "squad")
        with pytest.raises(ckm.CheckpointConflict):
            ckm.delete_managed(tmp_path, "k")
    assert not ckm.is_busy(p)


def test_list_and_describe_skip_junk(tmp_path):
    d = tmp_path / "checkpoints"
    d.mkdir()
    (d / "junk.json").write_text("[1,2]")
    (d / "bad name.json").write_text("{}")
    Checkpoint(d / "a.json").begin("task a " * 40, "squad", "ra", n=2)
    rows = ckm.list_managed(tmp_path)
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["task"].endswith("…") and len(rows[0]["task"]) <= 120
    assert ckm.list_managed(tmp_path / "nope") == []
    assert ckm.describe(d / "missing.json") is None


# ---------------------------------------------------------------- POST /api/chat
def test_squad_checkpoint_created_and_resumed(client):
    r1 = _post(client, checkpoint_id="trip-1")
    assert r1.status_code == 200, r1.text
    j1 = r1.json()
    assert j1["checkpoint_id"] == "trip-1" and j1["answer"].startswith("LEAD MERGED")
    f = client.ckdir / "trip-1.json"
    assert f.exists()
    calls = dict(client.llm.calls)
    r2 = _post(client, checkpoint_id="trip-1")
    assert r2.status_code == 200
    assert r2.json()["run_id"] == j1["run_id"] and r2.json()["answer"] == j1["answer"]
    assert client.llm.calls == calls  # finished run returned from the checkpoint, no model calls


def test_partial_run_resumes_only_missing_work(client):
    f = client.ckdir / "half.json"
    ck = Checkpoint(f)
    ck.begin("plan a trip", "squad", "orig-run", n=2)
    ck.put("subtasks", [{"role": "A", "focus": "alpha"}, {"role": "B", "focus": "beta"}])
    ck.put("worker.0", "alpha done earlier")
    r = _post(client, checkpoint_id="half")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["run_id"] == "orig-run"  # resume logs under the original run
    assert client.llm.calls["plan"] == 0 and client.llm.calls["worker"] == 1
    assert j["agents"][0]["result"] == "alpha done earlier"
    kinds = [e["kind"] for e in client.get("/api/runs/orig-run").json()]
    assert "checkpoint.resume" in kinds


def test_different_task_is_409(client):
    assert _post(client, checkpoint_id="c1").status_code == 200
    r = _post(client, checkpoint_id="c1", message="something else")
    assert r.status_code == 409
    assert "different squad task" in r.json()["detail"] and "c1" in r.json()["detail"]
    r = _post(client, checkpoint_id="c1", mode="deep", first_answer="x")
    assert r.status_code == 409


@pytest.mark.parametrize("bad", ["../etc", "a" * 65, "", "a.b", "fleet-hub-x"])
def test_bad_checkpoint_id_is_400(client, bad):
    r = _post(client, checkpoint_id=bad)
    assert r.status_code == 400
    assert not client.ckdir.exists() or not any(client.ckdir.iterdir())


def test_chat_mode_rejects_checkpoint(client):
    assert _post(client, mode="chat", checkpoint_id="x").status_code == 400
    assert _post(client, mode="nope", checkpoint_id="x").status_code == 400


def test_no_checkpoint_id_writes_nothing(client):
    assert _post(client).status_code == 200
    assert not client.ckdir.exists()


def test_deep_checkpoint_resume(client):
    body = dict(mode="deep", first_answer="first draft", squad_size=2, checkpoint_id="deep-1")
    r1 = _post(client, **body)
    assert r1.status_code == 200, r1.text
    calls = dict(client.llm.calls)
    assert calls["reviewer"] == 2
    r2 = _post(client, **body)
    assert r2.status_code == 200 and r2.json()["run_id"] == r1.json()["run_id"]
    assert client.llm.calls == calls
    meta = client.get("/api/checkpoints/deep-1", headers=ADMIN).json()
    assert meta["mode"] == "deep" and meta["done"] is True


def test_background_run_that_dies_resumes_on_repost(client):
    """A background run killed mid-flight frees its checkpoint; re-posting continues it."""
    from avrone import server
    client.core.llm = client.llm = CountingLLM(hang="beta")
    r = _post(client, checkpoint_id="bg", background=True)
    assert r.status_code == 200 and r.json()["status"] == "running" and r.json()["checkpoint_id"] == "bg"
    run_id = r.json()["run_id"]
    f = client.ckdir / "bg.json"

    def worker0_saved():
        try:
            return Checkpoint(f).get("worker.0") is not None
        except Exception:
            return False
    for _ in range(200):
        if worker0_saved():
            break
        client.portal.call(asyncio.sleep, 0.01)
    assert worker0_saved()
    # still running: the id is busy
    assert _post(client, checkpoint_id="bg").status_code == 409
    assert client.get("/api/checkpoints/bg", headers=ADMIN).json()["busy"] is True
    assert client.delete("/api/checkpoints/bg", headers=ADMIN).status_code == 409

    async def kill():
        for t in list(server.state["tasks"]):
            t.cancel()
        await asyncio.sleep(0.05)
    client.portal.call(kill)
    assert client.get("/api/checkpoints/bg", headers=ADMIN).json()["busy"] is False

    client.core.llm = client.llm = CountingLLM()
    r2 = _post(client, checkpoint_id="bg")
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id
    assert client.llm.calls["plan"] == 0 and client.llm.calls["worker"] == 1  # only beta re-ran


# ---------------------------------------------------------------- admin routes
@pytest.mark.parametrize("method,path", [
    ("get", "/api/checkpoints"), ("get", "/api/checkpoints/x"), ("delete", "/api/checkpoints/x")])
def test_checkpoint_routes_are_admin_gated(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"x-admin-token": "nope"}).status_code == 401


def test_checkpoint_routes_403_when_admin_off(client, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "admin_token", "")
    assert client.get("/api/checkpoints", headers=ADMIN).status_code == 403


def test_list_get_delete(client):
    assert client.get("/api/checkpoints", headers=ADMIN).json() == []
    _post(client, checkpoint_id="one")
    _post(client, checkpoint_id="two", message="second task")
    rows = client.get("/api/checkpoints", headers=ADMIN).json()
    assert {r["id"] for r in rows} == {"one", "two"}
    row = next(r for r in rows if r["id"] == "two")
    assert row["mode"] == "squad" and row["task"] == "second task" and row["done"] is True
    assert row["done_steps"] >= 3 and row["updated"]
    full = client.get("/api/checkpoints/one", headers=ADMIN).json()
    assert full["id"] == "one" and full["run_id"] and "result" in full["keys"]
    assert full["answer_preview"].startswith("LEAD MERGED") and full["owner"] is None
    assert client.get("/api/checkpoints/zzz", headers=ADMIN).status_code == 404
    assert client.delete("/api/checkpoints/one", headers=ADMIN).json() == {"deleted": "one"}
    assert client.delete("/api/checkpoints/one", headers=ADMIN).status_code == 404
    assert client.get("/api/checkpoints/one", headers=ADMIN).status_code == 404
    assert [r["id"] for r in client.get("/api/checkpoints", headers=ADMIN).json()] == ["two"]
    # after delete the id is free for any task
    assert _post(client, checkpoint_id="one", message="new task").status_code == 200


@pytest.mark.parametrize("bad", ["a.b", "a" * 161, "%2E%2E"])
def test_admin_bad_id_is_400(client, bad):
    assert client.get(f"/api/checkpoints/{bad}", headers=ADMIN).status_code == 400
    assert client.delete(f"/api/checkpoints/{bad}", headers=ADMIN).status_code == 400
