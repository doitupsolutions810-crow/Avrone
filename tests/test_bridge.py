"""Learning bridge: run-scoped blackboard, tools, persistence, fleet relay hooks, API."""
import asyncio
import json
import threading

import pytest

from avrone import bridge as bridge_mod
from avrone.bridge import Bridge, BridgeError, compact, for_core
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry
from avrone.trinity import AgentContext, TrinityCore


def _core(tmp_path, llm=None, **over):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 3
    for k, v in over.items():
        setattr(s, k, v)
    reg = ToolRegistry()
    bridge_mod.register_bridge_tools(reg)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir), tools=reg)


# ------------------------------------------------------------------ posting + filters

def test_post_and_read_filters(tmp_path):
    b = Bridge("r1", tmp_path)
    f = b.post("Alpha-1", "finding", "port 22 is open", role="Recon")
    q = b.post("Beta-2", "question", "is 22 filtered upstream?")
    a = b.reply("Alpha-1", [q["id"]], "no, reachable from the LAN")
    c = b.post("Beta-2", "conclusion", "port 22 open on the LAN", refs=[f["id"], a["id"]])
    assert [p["id"] for p in b.posts()] == [f["id"], q["id"], a["id"], c["id"]]
    assert set(f) >= {"id", "run_id", "agent", "role", "kind", "text", "refs", "ts", "origin_node"}
    assert f["origin_node"] == "local" and f["run_id"] == "r1" and f["role"] == "Recon"
    assert [p["kind"] for p in b.read(kind="question")] == ["question"]
    assert {p["agent"] for p in b.read(agent="beta")} == {"Beta-2"}
    assert [p["id"] for p in b.read(agent="recon")] == [f["id"]]  # role match
    assert [p["id"] for p in b.read(since=q["id"])] == [a["id"], c["id"]]
    assert [p["id"] for p in b.read(query="LAN")] == [a["id"], c["id"]]
    assert len(b.read(limit=1)) == 1 and b.read(limit=1)[0]["id"] == c["id"]
    assert a["refs"] == [q["id"]]
    assert b.unresolved() == []
    line = compact(b.read(kind="answer"))
    assert line.startswith(f"[{a['id']}] answer Alpha-1 re {q['id']}:")
    assert compact([]) == "(no posts)"


def test_post_validation(tmp_path):
    b = Bridge("r1", tmp_path)
    with pytest.raises(BridgeError):
        b.post("a", "rumour", "x")
    with pytest.raises(BridgeError):
        b.post("a", "finding", "   ")
    with pytest.raises(BridgeError):
        b.post("a", "finding", "x", refs=["nope"])
    with pytest.raises(BridgeError):
        b.reply("a", [], "x")
    p = b.post("a", "FINDING", "x", refs="")
    assert p["kind"] == "finding" and p["refs"] == []


def test_caps_text_and_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "3")
    b = Bridge("r1", tmp_path)
    p = b.post("a", "finding", "x" * 9000)
    assert len(p["text"]) == bridge_mod.MAX_TEXT and p["text"].endswith("...")
    ids = [b.post("a", "finding", f"n{i}")["id"] for i in range(5)]
    assert [x["id"] for x in b.posts()] == ids[-3:]
    # file is compacted, replay keeps the last 3
    for i in range(10):
        b.post("a", "finding", f"m{i}")
    assert len(Bridge("r1", tmp_path).posts()) == 3
    lines = (tmp_path / "bridge" / "r1.jsonl").read_text().splitlines()
    assert len(lines) <= 6
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "junk")
    assert bridge_mod.max_posts() == 500
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "999999")
    assert bridge_mod.max_posts() == bridge_mod.HARD_MAX_POSTS


def test_rounds_env(monkeypatch):
    monkeypatch.delenv("AVRONE_BRIDGE_ROUNDS", raising=False)
    assert bridge_mod.bridge_rounds() == 1
    for raw, want in (("0", 0), ("2", 2), ("9", 3), ("-4", 0), ("x", 1)):
        monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", raw)
        assert bridge_mod.bridge_rounds() == want


def test_persistence_and_corrupt_replay(tmp_path):
    b = Bridge("run/../x", tmp_path)
    p1 = b.post("a", "finding", "one")
    p2 = b.post("b", "correction", "two", refs=[p1["id"]])
    path = tmp_path / "bridge" / f"{bridge_mod.safe_run_id('run/../x')}.jsonl"
    assert path.exists() and path.parent == tmp_path / "bridge"
    with path.open("a") as f:
        f.write('{"id": "trunc", "kind": "finding", "te\n')
        f.write("not json\n[1,2]\n")
        f.write(json.dumps({"id": "badkind", "kind": "gossip", "text": "x"}) + "\n")
        f.write(json.dumps({"id": "bad id!", "kind": "finding", "text": "x"}) + "\n")
        f.write("\n")
    again = Bridge("run/../x", tmp_path)
    assert [p["id"] for p in again.posts()] == [p1["id"], p2["id"]]
    assert again.get(p2["id"])["refs"] == [p1["id"]]
    p3 = again.post("c", "conclusion", "three", refs=[p2["id"]])
    assert [p["id"] for p in Bridge("run/../x", tmp_path).posts()][-1] == p3["id"]


def test_memory_only_bridge_and_thread_safety():
    b = Bridge("mem")
    def worker(n):
        for i in range(50):
            b.post(f"w{n}", "finding", f"{n}-{i}")
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(b) == 400 and len({p["id"] for p in b.posts()}) == 400


def test_wave1_shares_bridge_and_digest(tmp_path):
    assert bridge_mod.bridge_run_id("abc-wave1") == "abc"
    assert bridge_mod.bridge_run_id("-wave1") == "-wave1"
    core = _core(tmp_path)
    assert for_core(core, "abc-wave1") is for_core(core, "abc")
    b = for_core(core, "abc")
    assert b.digest() == ""
    b.post("a", "finding", "f")
    assert "no conclusions" in b.digest()
    q = b.post("a", "question", "why?")
    c = b.post("b", "correction", "fix it")
    b.post("c", "conclusion", "agreed", refs=[c["id"]])
    d = b.digest()
    assert "What agents concluded together" in d and "agreed" in d
    assert "Unresolved" in d and q["id"] in d.split("Unresolved")[1]
    assert c["id"] not in d.split("Unresolved")[1]
    assert len(b.digest(max_chars=40)) == 40


def test_export_merge_remote(tmp_path):
    a = Bridge("r", tmp_path / "a", node_id="node-a")
    p = a.post("x", "finding", "local fact")
    a2 = a.post("x", "conclusion", "local conclusion")
    assert [x["id"] for x in a.export()] == [p["id"], a2["id"]]
    assert [x["id"] for x in a.export(since=p["id"])] == [a2["id"]]
    assert len(a.export(since="unknown")) == 2
    b = Bridge("r", tmp_path / "b", node_id="node-b")
    mine = b.post("y", "finding", "b's own")
    forged = dict(mine, text="overwritten!", origin_node="node-a")
    stats = b.merge_remote(a.export() + [forged, {"junk": 1}, "x"], "node-a")
    assert stats == {"merged": 2, "skipped": 1, "rejected": 2}
    assert b.get(mine["id"])["text"] == "b's own"
    assert b.get(p["id"])["origin_node"] == "node-a"
    # relayed again (and via a third node): still deduped, original origin kept
    stats2 = b.merge_remote([dict(a.get(p["id"]), origin_node=None)], "node-c")
    assert stats2["skipped"] == 1
    c = Bridge("r", tmp_path / "c", node_id="node-c")
    c.merge_remote(b.export(), "node-b")
    assert c.get(p["id"])["origin_node"] == "node-a" and c.get(mine["id"])["origin_node"] == "node-b"
    # persisted
    assert Bridge("r", tmp_path / "c").get(p["id"])["text"] == "local fact"


def test_codex_event_on_post(tmp_path):
    core = _core(tmp_path)
    for_core(core, "r9").post("A-1", "finding", "hello")
    evs = [e for e in core.codex.replay("r9") if e["kind"] == "bridge.post"]
    assert evs and evs[0]["agent"] == "A-1" and evs[0]["data"]["text"] == "hello"


# ------------------------------------------------------------------ tools (shared across depths)

def test_tools_same_bridge_any_depth(tmp_path):
    core = _core(tmp_path)
    core.active["W-1"] = {"role": "Worker", "status": "working"}
    w = AgentContext(core, "run7", "W-1", 0)
    sub = AgentContext(core, "run7", "Helper-5", 2)
    other = AgentContext(core, "run8", "Z-9", 0)

    async def go():
        out = await core.tools.call(w, "bridge_post", {"text": "found X", "kind": "finding"})
        pid = out.split("[")[1].split("]")[0]
        seen = await core.tools.call(sub, "bridge_read", {})
        rep = await core.tools.call(sub, "bridge_reply", {"refs": [pid], "text": "confirmed X"})
        bad = await core.tools.call(sub, "bridge_reply", {"refs": [], "text": "x"})
        bad2 = await core.tools.call(w, "bridge_post", {"text": "x", "kind": "gossip"})
        filt = await core.tools.call(w, "bridge_read", {"kind": "answer", "limit": "junk"})
        iso = await core.tools.call(other, "bridge_read", {})
        return out, pid, seen, rep, bad, bad2, filt, iso
    out, pid, seen, rep, bad, bad2, filt, iso = asyncio.run(go())
    assert "posted finding" in out and "found X" in seen and "W-1" in seen
    assert rep.startswith("replied answer") and pid in rep
    assert bad.startswith("ERROR") and "refs" in bad and bad2.startswith("ERROR")
    assert "confirmed X" in filt and "found X" not in filt
    assert iso == "(no posts)"
    assert for_core(core, "run7").posts()[0]["role"] == "Worker"
    assert core.tools.check("bridge_reply", {"text": "x"}) is not None


def test_extra_tools_register_bridge():
    from avrone.tools import builtin_registry
    from avrone.tools_extra import register_extra_tools
    r = builtin_registry()
    register_extra_tools(r)
    assert {"bridge_post", "bridge_read", "bridge_reply"} <= set(r.names())


def test_for_core_cache_bounded(tmp_path):
    core = _core(tmp_path)
    for i in range(bridge_mod._MAX_OPEN + 5):
        for_core(core, f"r{i}")
    assert len(core._bridges) == bridge_mod._MAX_OPEN
    assert bridge_mod.load_posts(tmp_path / "d", "nope") is None


# ------------------------------------------------------------------ API

def test_api_run_bridge(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        core = server.state["core"]
        b = for_core(core, "runA")
        p = b.post("A-1", "conclusion", "we agree")
        r = c.get("/api/runs/runA/bridge")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1 and body["posts"][0]["id"] == p["id"]
        # persisted bridge, not open in this core
        Bridge("runB", tmp_path / "data").post("B-1", "finding", "on disk")
        assert c.get("/api/runs/runB/bridge").json()["posts"][0]["text"] == "on disk"
        # a run with a log but no bridge -> empty; unknown -> 404; junk id -> 400
        core.codex.emit("runC", "run.start", "Trinity", prompt="x")
        assert c.get("/api/runs/runC/bridge").json() == {"run_id": "runC", "count": 0, "posts": []}
        assert c.get("/api/runs/ghost/bridge").status_code == 404
        assert c.get("/api/runs/a%20b/bridge").status_code == 400
