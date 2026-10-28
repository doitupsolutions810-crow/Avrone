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
    assert ckm.fleet_key("edge-1", "job").startswith("fleet-edge-1-") and ckm.fleet_key("edge-1", "job").endswith("-job")
    # no collision when both parts contain "-"
    assert ckm.fleet_key("a", "b-c") != ckm.fleet_key("a-b", "c")
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


# ---------------------------------------------------------------- fleet
from avrone.fleet import sign  # noqa: E402


@pytest.fixture()
def fclient(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "fleet-secret")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_MAX_HOPS",
              "AVRONE_FLEET_REQUIRE_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(server.settings, "max_steps", 2)
    with TestClient(server.app) as c:
        llm = CountingLLM()
        server.state["core"].llm = llm
        c.llm = llm
        c.ckdir = tmp_path / "data" / "checkpoints"
        yield c


def _frun(c, **payload):
    body = json.dumps({"mode": "squad", "prompt": "remote job", "n": 2, "hops": 1, "from": "edge1",
                       **payload}).encode()
    h = {"content-type": "application/json", **sign("fleet-secret", "POST", "/api/fleet/run", body)}
    return c.post("/api/fleet/run", content=body, headers=h)


def test_fleet_retried_job_resumes_under_sender_key(fclient):
    r1 = _frun(fclient, checkpoint_id="job7")
    assert r1.status_code == 200, r1.text
    assert r1.json()["checkpoint_id"] == "job7"
    f = fclient.ckdir / (ckm.fleet_key("edge1", "job7") + ".json")
    assert f.exists() and json.loads(f.read_text())["owner"] == "fleet:edge1"
    calls = dict(fclient.llm.calls)
    r2 = _frun(fclient, checkpoint_id="job7")  # the sender retries the same job
    assert r2.status_code == 200 and r2.json()["run_id"] == r1.json()["run_id"]
    assert fclient.llm.calls == calls
    # visible to the admin under its fleet key
    row = fclient.get("/api/checkpoints/" + ckm.fleet_key("edge1", "job7"), headers=ADMIN).json()
    assert row["owner"] == "fleet:edge1" and row["done"] is True


def test_fleet_senders_cannot_collide_or_read_each_other(fclient):
    assert _frun(fclient, checkpoint_id="same").status_code == 200
    calls = dict(fclient.llm.calls)
    r = _frun(fclient, checkpoint_id="same", **{"from": "edge2"})  # same id, other sender: own fresh run
    assert r.status_code == 200
    assert fclient.llm.calls["plan"] == calls["plan"] + 1
    assert (fclient.ckdir / (ckm.fleet_key("edge2", "same") + ".json")).exists()
    # a client cannot reach a fleet checkpoint through /api/chat
    r = fclient.post("/api/chat", json={"message": "remote job", "squad_size": 2,
                                        "checkpoint_id": "fleet-edge1-same"})
    assert r.status_code == 400


def test_fleet_checkpoint_conflict_and_bad_id(fclient):
    assert _frun(fclient, checkpoint_id="j").status_code == 200
    r = _frun(fclient, checkpoint_id="j", prompt="another job")
    assert r.status_code == 409 and "different squad task" in r.json()["detail"]
    assert _frun(fclient, checkpoint_id="../x").status_code == 400
    assert _frun(fclient, checkpoint_id="j", mode="chat").status_code == 400
    assert _frun(fclient, checkpoint_id=5).status_code == 400


def test_fleet_owner_mismatch_on_existing_file_is_409(fclient):
    # a file at the key not stamped for this sender (e.g. planted) is never resumed
    fclient.ckdir.mkdir(parents=True, exist_ok=True)
    Checkpoint(fclient.ckdir / (ckm.fleet_key("edge1", "p") + ".json")).begin("remote job", "squad", "x", n=2)
    r = _frun(fclient, checkpoint_id="p")
    assert r.status_code == 409 and "another owner" in r.json()["detail"]


class _FakeCore:
    def __init__(self):
        self.calls = []
        self.codex = type("C", (), {"emit": lambda *a, **k: None})()

    async def squad(self, task, n=None, **kw):
        self.calls.append(("squad", task, kw))
        return {"answer": "a", "run_id": "r"}

    async def deep(self, task, first=None, n=None, **kw):
        self.calls.append(("deep", task, kw))
        return {"report": "rep", "run_id": "r"}

    async def chat(self, task):
        return {"answer": "c"}


@pytest.fixture()
def fm(tmp_path, monkeypatch):
    from avrone.fleet import FleetManager
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_REQUIRE_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    f = FleetManager(_FakeCore(), tmp_path)
    f.posted = []

    async def fake_post(url, path, payload, timeout=30):
        f.posted.append(payload)
        return {"node": "edge", "path": ["edge"], "answer": "remote", "run_id": "rr"}
    f._post = fake_post
    f.pick = lambda target: {"node_id": "edge", "url": "http://edge.invalid", "load": 0}
    return f


def test_dispatch_signs_checkpoint_id_into_body(fm):
    asyncio.run(fm.dispatch(["edge"], "deep", "p", 0, checkpoint_id="abc"))
    assert fm.posted[-1]["checkpoint_id"] == "abc"
    asyncio.run(fm.dispatch(["edge"], "squad", "p", 0))
    assert "checkpoint_id" not in fm.posted[-1]
    with pytest.raises(ValueError):
        asyncio.run(fm.dispatch(["edge"], "squad", "p", 0, checkpoint_id="a/b"))
    with pytest.raises(ValueError):
        asyncio.run(fm.dispatch(["edge"], "chat", "p", 0, checkpoint_id="abc"))


def test_handle_run_forwards_checkpoint_id_down_the_route(fm):
    res = asyncio.run(fm.handle_run({"route": ["edge"], "mode": "squad", "prompt": "p", "hops": 1,
                                     "from": "parent", "checkpoint_id": "k"}))
    assert fm.posted[-1]["checkpoint_id"] == "k" and res["path"][0] == "hub"


def test_handle_run_local_deep_uses_managed_path(fm, tmp_path):
    res = asyncio.run(fm.handle_run({"mode": "deep", "prompt": "p", "hops": 1, "from": "kid",
                                     "checkpoint_id": "d1"}))
    kind, task, kw = fm.core.calls[-1]
    assert kind == "deep" and kw["checkpoint"] == tmp_path / "checkpoints" / (ckm.fleet_key("kid", "d1") + ".json")
    assert res["checkpoint_id"] == "d1" and res["answer"] == "rep"
    assert not ckm.is_busy(kw["checkpoint"])  # released after the run
    asyncio.run(fm.handle_run({"mode": "squad", "prompt": "p", "hops": 1, "from": "kid"}))
    assert fm.core.calls[-1][2] == {}  # no id: no checkpoint kwarg (old cores keep working)


def test_remote_spawn_tool_passes_checkpoint_id(fm):
    from types import SimpleNamespace
    from avrone.fleet import t_remote_spawn
    ctx = SimpleNamespace(core=SimpleNamespace(fleet=fm, fleet_hops=0), run_id="r", name="A", depth=0)
    asyncio.run(t_remote_spawn(ctx, {"task": "t", "node": "edge", "mode": "squad", "checkpoint_id": "z"}))
    assert fm.posted[-1]["checkpoint_id"] == "z"


# ---------------------------------------------------------------- MCP
class _McpCore:
    def __init__(self, tmp_path):
        from types import SimpleNamespace
        self.settings = SimpleNamespace(data_dir=tmp_path)
        self.calls = []

    async def squad(self, task, size=None, **kw):
        self.calls.append(("squad", task, kw))
        if kw.get("checkpoint"):
            assert ckm.is_busy(kw["checkpoint"])
            Checkpoint(kw["checkpoint"]).begin(task, "squad", "r1")
        return {"answer": "ok", "run_id": "r1", "agents": []}

    async def deep(self, task, first=None, **kw):
        self.calls.append(("deep", task, kw))
        return {"answer": "deep", "run_id": "r2"}

    async def chat(self, task):
        return "c"


def test_mcp_squad_and_deep_accept_checkpoint_id(tmp_path):
    from avrone import mcp_server as ms
    core = _McpCore(tmp_path)

    async def factory():
        return core, {}
    srv = ms.AvroneMCPServer(factory)
    out = asyncio.run(srv.call_tool("avrone_squad", {"task": "t", "checkpoint_id": "m1"}))
    assert out["isError"] is False and out["structuredContent"]["checkpoint_id"] == "m1"
    path = tmp_path / "checkpoints" / "m1.json"
    assert core.calls[-1][2] == {"checkpoint": path} and not ckm.is_busy(path)
    out = asyncio.run(srv.call_tool("avrone_squad", {"task": "other", "checkpoint_id": "m1"}))
    assert out["isError"] and "different squad task" in out["content"][0]["text"]
    out = asyncio.run(srv.call_tool("avrone_squad", {"task": "t", "checkpoint_id": "../x"}))
    assert out["isError"] and "checkpoint_id" in out["content"][0]["text"]
    asyncio.run(srv.call_tool("avrone_deep", {"task": "q", "checkpoint_id": "m2"}))
    assert core.calls[-1] == ("deep", "q", {"checkpoint": tmp_path / "checkpoints" / "m2.json"})
    asyncio.run(srv.call_tool("avrone_squad", {"task": "t"}))
    assert core.calls[-1][2] == {}
    schemas = {t["name"]: t["inputSchema"]["properties"] for t in ms.TOOLS}
    assert "checkpoint_id" in schemas["avrone_squad"] and "checkpoint_id" in schemas["avrone_deep"]
    assert "checkpoint_id" not in schemas["avrone_chat"]


# ---------------------------------------------------------------- CLI
def _cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(tmp_path / "np"))
    monkeypatch.setenv("AVRONE_MCP_CONFIG", str(tmp_path / "none.json"))


def test_cli_checkpoint_id(tmp_path, monkeypatch, capsys):
    from avrone import cli
    _cli_env(tmp_path, monkeypatch)
    base = ["--quiet", "--no-extras", "-n", "2", "--checkpoint-id", "cli-1"]
    assert cli.main(["squad", "hello", *base]) == 0
    f = tmp_path / "d" / "checkpoints" / "cli-1.json"
    assert Checkpoint(f).get("result") is not None
    first = capsys.readouterr().out
    assert cli.main(["squad", "hello", *base]) == 0
    assert capsys.readouterr().out == first  # resumed: same stored answer
    assert cli.main(["squad", "other task", *base]) == 1
    assert "different squad task" in capsys.readouterr().err
    assert cli.main(["squad", "hello", "--quiet", "--no-extras", "--checkpoint-id", "a/b"]) == 1
    assert cli.main(["deep", "q?", "--first-answer", "a", "--quiet", "--no-extras", "-n", "1",
                     "--checkpoint-id", "cli-d"]) == 0
    assert (tmp_path / "d" / "checkpoints" / "cli-d.json").exists()


def test_cli_checkpoint_flags_are_exclusive():
    from avrone import cli
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["squad", "x", "--checkpoint", "p", "--checkpoint-id", "i"])
    assert cli.build_parser().parse_args(["deep", "x", "--checkpoint-id", "i"]).checkpoint_id == "i"
    assert cli.build_parser().parse_args(["squad", "x"]).checkpoint_id is None
