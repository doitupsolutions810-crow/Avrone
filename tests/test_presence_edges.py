"""Offline edge cases for presence flattening, filtering, and name resolution."""
import pytest

from avrone.presence import MAX_AGENTS, agents, find_agent, flatten, render


def test_agents_caps_at_max_and_prioritizes_working_then_recent():
    active = {}
    for i in range(MAX_AGENTS + 8):
        active[f"Agent-{i:02d}"] = {
            "status": "working" if i in (0, 1) else "idle",
            "started": float(i),
            "role": "worker",
            "run_id": "r",
            "task": f"task-{i}",
            "depth": 0,
        }
    rows = agents(type("Core", (), {"active": active})())
    assert len(rows) == MAX_AGENTS
    assert rows[0]["name"] == "Agent-01"
    assert rows[1]["name"] == "Agent-00"


def test_flatten_builds_nested_routes_without_mutating_child_data():
    child = {
        "node_id": "edge",
        "status": "degraded",
        "url": "https://edge.example",
        "env": {"device": {"hostname": "edge-host"}},
        "agents": [{"name": "Builder"}],
        "children": {},
    }
    root = {"node_id": "root", "env": {}, "agents": [], "children": {
        "router": {"node_id": "router", "env": {}, "agents": [], "children": {"edge": child}}
    }}
    rows = flatten(root)
    assert [r["route"] for r in rows] == ["", "router", "router/edge"]
    assert rows[-1]["node_id"] == "edge"
    assert rows[-1]["status"] == "degraded"
    assert child["status"] == "degraded"


def test_find_agent_prefers_one_live_match_over_idle_duplicate():
    rows = [
        {"route": "", "agents": [{"name": "Builder-1", "status": "idle"}]},
        {"route": "edge", "agents": [{"name": "Builder-1", "status": "working"}]},
    ]
    assert find_agent(rows, "Builder-1") == ("edge", "Builder-1")


def test_find_agent_distinguishes_unknown_routes_and_ambiguous_live_names():
    rows = [
        {"route": "", "agents": [{"name": "Same", "status": "working"}]},
        {"route": "edge", "agents": [{"name": "Same", "status": "working"}]},
    ]
    with pytest.raises(LookupError, match="no node at route"):
        find_agent(rows, "missing/Builder")
    with pytest.raises(LookupError, match="ambiguous"):
        find_agent(rows, "Same")


def test_render_only_live_excludes_idle_agents():
    rows = [{
        "route": "",
        "node_id": "local",
        "status": "online",
        "env": {"device": {"hostname": "host", "os": "Linux"}, "runner": {"kind": "local"}},
        "agents": [
            {"name": "Working", "role": "worker", "status": "working", "run_id": "r", "task": "active"},
            {"name": "Idle", "role": "worker", "status": "idle", "run_id": "r", "task": "waiting"},
        ],
    }]
    all_text = render(rows)
    live_text = render(rows, only_live=True)
    assert "Working" in all_text and "Idle" in all_text
    assert "Working" in live_text and "Idle" not in live_text
