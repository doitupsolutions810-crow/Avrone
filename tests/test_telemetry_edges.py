"""Offline edge tests for telemetry.py: odd logs, fleet map, node health / governor state."""
import json
import os
import time
from types import SimpleNamespace

from avrone import telemetry as tm
from avrone.governor import Governor


def _core(tmp_path, **over):
    log_dir = tmp_path / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    base = dict(
        codex=SimpleNamespace(log_dir=log_dir),
        active={"W-1": {"status": "working"}, "W-2": {"status": "done"}},
        settings=SimpleNamespace(llm_model="m", llm_base_url="u", lead_model="", max_parallel=4,
                                 governor=True, min_parallel=1, thermal_max_c=85, data_dir=tmp_path),
        runner=SimpleNamespace(kind="local", workspace=tmp_path / "ws"),
        tools=SimpleNamespace(list=lambda: [{"name": "shell", "source": "builtin"},
                                            {"name": "ext", "source": "plugin"}]),
        governor=Governor(4, min_parallel=1, thermal_max_c=85, ttl_s=0,
                          readers={"load": lambda: 0.1, "mem_pct": lambda: 20.0,
                                   "disk_free_pct": lambda: 50.0, "temp_c": lambda: 92.0}),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _write(log_dir, name, events, mtime=None, junk=False):
    p = log_dir / f"{name}.jsonl"
    lines = [json.dumps(e) for e in events]
    if junk:
        lines.insert(1, "{not json")
    p.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def ev(kind, rid="r1", agent="Trinity", ts=None, **data):
    return {"run_id": rid, "kind": kind, "agent": agent, "ts": ts or time.time(), "data": data}


def test_telemetry_counts_errors_throttles_resumes_and_skips_junk(tmp_path):
    core = _core(tmp_path)
    now = time.time()
    _write(core.codex.log_dir, "r1", [
        ev("run.start", mode="squad", prompt="p", ts=now - 5),
        ev("agent.spawn", agent="A-1", role="A"),
        ev("tool.call", agent="A-1", tool="shell"),
        ev("tool.result", agent="A-1", tool="shell", output="ERROR: boom"),
        ev("tool.result", agent="A-1", tool="python", output="no matching call"),
        ev("error", message="x"),
        ev("governor.throttle", agent="Governor", workers=4, parallel=1, reason="throttled by temp_c"),
        ev("checkpoint.resume", workers_done=2, workers=4),
        ev("run.done", ts=now),
        ev("tool.call", ts=now - 10 ** 6, tool="ancient"),  # outside the window
    ], junk=True)
    _write(core.codex.log_dir, "old", [ev("run.start", rid="old", mode="chat", ts=now - 10 ** 6)],
           mtime=now - 10 ** 6)
    t = tm.telemetry(core, 3600)
    assert t["runs"]["total"] == 1 and t["runs"]["completed"] == 1 and t["runs"]["p50_s"] == 5.0
    assert t["tools"]["shell"]["calls"] == 1 and t["tools"]["shell"]["errors"] == 1
    assert t["tools"]["shell"]["avg_s"] is not None  # call/result pair timed
    assert "ancient" not in t["tools"]
    assert t["errors"] == 1
    assert t["agents"]["active_now"] == ["W-1"]
    assert t["resilience"]["throttles"] == 1 and t["resilience"]["checkpoint_resumes"] == 1
    assert t["resilience"]["recent_throttles"][0]["parallel"] == 1
    node = t["node"]
    assert node["enabled"] and node["health"]["temp_c"] == 92.0 and node["factors"]["temp_c"] == 0.0
    assert node["max_parallel"] == 4


def test_empty_window_has_no_percentiles(tmp_path):
    t = tm.telemetry(_core(tmp_path), 60)
    assert t["runs"]["p50_s"] is None and t["resilience"]["throttles"] == 0


def test_node_health_builds_a_governor_when_core_has_none(tmp_path):
    core = _core(tmp_path, governor=None)
    st = tm.node_health(core)
    assert st["max_parallel"] == 4 and "health" in st


def test_node_health_never_raises(tmp_path):
    class Broken:
        def state(self):
            raise RuntimeError("sensor bus fried")
    st = tm.node_health(_core(tmp_path, governor=Broken()))
    assert st["enabled"] is False and "sensor bus fried" in st["error"]


class FakeFleet:
    enabled = True
    node_id = "edge-a"
    parent = "hq"

    def list_nodes(self):
        return [
            {"node_id": "child1", "status": "online", "url": "http://c1",
             "presence": {"node_id": "child1", "agents": [{"name": "Scout-1", "status": "working"},
                                                          {"name": "Old-2", "status": "done"}],
                          "children": {"grand": {"node_id": "grand", "agents": [
                              {"name": "Deep-3", "status": "working"}],
                              "children": {"leaf": {"node_id": "leaf", "agents": []}}}}}},
            {"node_id": "child2", "status": "offline", "url": "http://c2"},
        ]


def test_system_map_with_fleet_and_unknown_parent(tmp_path):
    core = _core(tmp_path, fleet=FakeFleet())
    _write(core.codex.log_dir, "r1", [
        ev("agent.spawn", agent="Sub-2", role="Sub", parent="Ghost-9"),
        ev("agent.spawn", agent="A-1", role="A"),
        ev("tool.call", agent="A-1", tool="shell"),
        ev("tool.call", agent="A-1", tool="mystery"),
        ev("lead.merge", agent="Lead"),
    ])
    g = tm.system_map(core, 3600)
    ids = {n["id"] for n in g["nodes"]}
    assert {"fleet", "fleet:parent", "node:child1", "node:child2", "node:grand", "node:leaf",
            "ragent:child1/Scout-1", "ragent:grand/Deep-3", "tools:plugin", "tool:mystery"} <= ids
    assert "ragent:child1/Old-2" not in ids
    edges = {(e["from"], e["to"]): e["weight"] for e in g["edges"]}
    assert edges[("fleet", "fleet:parent")] == 1
    assert edges[("node:child1", "node:grand")] == 1 and edges[("node:grand", "node:leaf")] == 1
    assert edges[("tool:shell", "runner")] == 1 and edges[("core", "model:lead")] == 1
    # the parent edge pointing at a role that never spawned in the window gets a node
    assert "role:Ghost" in ids and edges[("role:Ghost", "role:Sub")] == 1
    mer = tm.to_mermaid(g, min_weight=1)
    assert "n_fleet -->|1| n_fleet_parent" in mer
    assert "n_core --> n_tools_builtin" not in mer  # zero-weight edges are filtered by min_weight
    assert "n_core --> n_tools_builtin" in tm.to_mermaid(g)


def test_system_map_fleet_without_parent_or_disabled(tmp_path):
    f = FakeFleet()
    f.parent = ""
    g = tm.system_map(_core(tmp_path, fleet=f), 60)
    assert "fleet:parent" not in {n["id"] for n in g["nodes"]}
    f.enabled = False
    g = tm.system_map(_core(tmp_path, fleet=f), 60)
    assert "fleet" not in {n["id"] for n in g["nodes"]}
