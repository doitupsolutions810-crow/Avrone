"""Round 10 coverage: fleet edges - thermal migration guards, agent-home scan filters,
inbound agentpack/memory refusals, outbound privileged gate order, lifecycle tasks."""
import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL",
              "AVRONE_FLEET_PRIVILEGED", "AVRONE_AGENT_MIGRATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def fm(tmp_path):
    core = MagicMock()
    core.active = {}
    core.settings = SimpleNamespace(workspace=str(tmp_path / "ws"), data_dir=str(tmp_path / "d"))
    return FleetManager(core, tmp_path / "d")


# ------------------------------------------------------------------ thermal migration
def test_thermal_migrate_off_or_cool(fm, monkeypatch):
    assert run(fm.thermal_migrate()) == []
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    fm.core.governor = SimpleNamespace(is_hot=lambda: False)
    assert run(fm.thermal_migrate()) == []
    fm.core.governor = SimpleNamespace(is_hot=lambda: (_ for _ in ()).throw(RuntimeError("sensor")))
    assert run(fm.thermal_migrate()) == []


def test_thermal_migrate_busy_check_error_keeps_agent_home(fm, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    fm.core.governor = SimpleNamespace(is_hot=lambda: True)
    monkeypatch.setattr(fm, "idle_agents", lambda now=None: ["a1"])
    monkeypatch.setattr(fm, "_busy_agent_ids", lambda: (_ for _ in ()).throw(RuntimeError()))
    called = []

    async def migrate(*a, **k):
        called.append(a)
    monkeypatch.setattr(fm, "migrate_agent", migrate)
    out = run(fm.thermal_migrate())
    assert called == [] and all(r.get("ok") is not True for r in out)


def test_idle_agents_filters(fm, tmp_path, monkeypatch):
    from avrone import agentfs
    assert fm.idle_agents() == []  # no agents root yet
    root = agentfs.agents_root(tmp_path / "ws")
    root.mkdir(parents=True)
    for a in ("idle1", "busy1", "Bad Name"):
        (root / a).mkdir()
    (root / "afile").write_text("x")
    (root / "link").symlink_to(root / "idle1")
    fm.core.active = {"w": {"status": "working", "agent_id": "busy1"}}
    monkeypatch.setattr(fleet_mod.time, "time", lambda: time.time_ns() / 1e9 + 10 ** 6)
    assert fm.idle_agents() == ["idle1"]


def test_workspace_missing(fm):
    fm.core.settings = SimpleNamespace(workspace=None)
    with pytest.raises(RuntimeError):
        fm._workspace()


# ------------------------------------------------------------------ inbound refusals
def test_receive_agentpack_refusals(fm, monkeypatch):
    with pytest.raises(PermissionError):
        fm.receive_agentpack({"capability": "agent:migrate", "from": "n1", "privileged": True})
    with pytest.raises(PermissionError):  # migration off
        fm.receive_agentpack({"capability": "agent:migrate", "from": "n1"})
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    with pytest.raises(ValueError, match="sender"):
        fm.receive_agentpack({"capability": "agent:migrate", "from": "root"})
    with pytest.raises(ValueError, match="base64"):
        fm.receive_agentpack({"capability": "agent:migrate", "from": "n1", "bundle": "!!not b64!!"})


def test_receive_memory_refusals(fm):
    base = {"capability": "memory:sync", "from": "n1"}
    with pytest.raises(PermissionError):
        fm.receive_memory({"capability": "agent:migrate", "from": "n1", "entries": []})
    with pytest.raises(ValueError, match="own id"):
        fm.receive_memory({**base, "from": "root", "entries": []})
    with pytest.raises(ValueError, match="list"):
        fm.receive_memory({**base, "entries": "x"})
    with pytest.raises(ValueError, match="too many"):
        fm.receive_memory({**base, "entries": [{}] * (fleet_mod.MEMORY_SYNC_MAX_ENTRIES + 1)})
    out = fm.receive_memory({**base, "entries": ["junk", {"ns": "fleet:x/y"}, {"ns": 5}]})
    assert out["rejected"] == 3 and out["namespaces"] == []


def test_push_memory_needs_parent(fm):
    with pytest.raises(RuntimeError, match="parent"):
        run(fm.push_memory([], "root"))


# ------------------------------------------------------------------ outbound privileged gate
def test_check_outbound_privileged_order(fm, monkeypatch):
    def code(**kw):
        try:
            fm.check_outbound(**kw)
        except GatewayRefused as e:
            return e.code
        return None
    assert code(kind="queue", capability="priv:shell", hops=0, first_hop="n1", privileged=True) == "privileged_capability"
    assert code(kind="run", capability="priv:shell", hops=0, first_hop="n1", privileged=True) == "privileged_disabled"
    fm.privileged = True
    fm.privileged_max_hops = 1
    assert code(kind="run", capability="priv:shell", hops=1, first_hop="n1", privileged=True) == "privileged_hops"
    assert code(kind="run", capability="priv:shell", hops=0, first_hop="auto", privileged=True) == "privileged_target"
    monkeypatch.setattr(fm, "allowed", lambda n: False)
    assert code(kind="run", capability="priv:shell", hops=0, first_hop="n1", privileged=True) == "target_not_allowed"
    monkeypatch.setattr(fm, "allowed", lambda n: True)
    assert code(kind="run", capability="priv:shell", hops=0, first_hop="n1", privileged=True) is None


# ------------------------------------------------------------------ lifecycle
def test_start_stop_thermal_task(fm, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    monkeypatch.setattr(fleet_mod, "migrate_interval", lambda: 3600.0)

    async def go():
        fm.start()
        t = fm._thermal_task
        assert t is not None
        await fm.stop()
        await asyncio.sleep(0)
        return t.cancelled() or t.done(), fm._thermal_task
    done, after = run(go())
    assert done and after is None
