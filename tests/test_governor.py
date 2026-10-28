"""Offline tests for the health/thermal governor: every sensor read is injected (fake)."""
import asyncio
import json

import pytest

from avrone import governor as gv
from avrone.codex import Codex
from avrone.config import Settings
from avrone.governor import Governor, NodeHealth
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore


def fake(load=0.2, mem=30.0, disk=60.0, temp=45.0):
    return {"load": lambda: load, "mem_pct": lambda: mem,
            "disk_free_pct": lambda: disk, "temp_c": lambda: temp}


def boom():
    raise OSError("sensor gone")


def gov(max_parallel=8, **kw):
    readers = kw.pop("readers", None) or fake()
    kw.setdefault("min_parallel", 1)
    kw.setdefault("thermal_max_c", 85)
    return Governor(max_parallel, readers=readers, ttl_s=0, **kw)


def test_cool_idle_node_gets_full_parallelism():
    g = gov(readers=fake())
    assert g.target() == 8
    assert g.last_reason == "healthy"
    assert g.throttle_count == 0


def test_hot_node_throttles_to_floor():
    assert gov(readers=fake(temp=90)).target() == 1
    assert gov(readers=fake(temp=85)).target() == 1


def test_warm_node_throttles_partially():
    # soft band 70..85C: 77.5C is half way -> 1 + round(7 * 0.5) = 5 (banker's rounding of 4.5 -> 4)
    t = gov(readers=fake(temp=77.5)).target()
    assert 1 < t < 8
    assert gov(readers=fake(temp=70)).target() == 8


@pytest.mark.parametrize("readers", [fake(load=3.0), fake(mem=97), fake(disk=1.0)])
def test_loaded_or_starved_node_throttles(readers):
    g = gov(readers=readers)
    assert g.target() == 1
    assert g.last_reason.startswith("throttled by")
    assert g.throttle_count == 1


def test_worst_sensor_wins():
    g = gov(readers=fake(load=1.5, mem=50, temp=40))  # load half way -> about half parallelism
    assert g.target() in (4, 5)
    assert "load" in g.last_reason


def test_floor_env_knob(monkeypatch):
    monkeypatch.setenv("AVRONE_MIN_PARALLEL", "3")
    g = Governor(8, readers=fake(temp=99), thermal_max_c=85, ttl_s=0)
    assert g.floor == 3 and g.target() == 3
    # floor never exceeds max_parallel and never drops below 1
    assert Governor(2, min_parallel=9, readers=fake()).floor == 2
    assert Governor(4, min_parallel=0, readers=fake()).floor == 1


def test_thermal_max_env_knob(monkeypatch):
    monkeypatch.setenv("AVRONE_THERMAL_MAX_C", "60")
    g = Governor(6, min_parallel=1, readers=fake(temp=62), ttl_s=0)
    assert g.thermal_max_c == 60 and g.target() == 1
    monkeypatch.setenv("AVRONE_THERMAL_MAX_C", "junk")
    monkeypatch.setenv("AVRONE_MIN_PARALLEL", "junk")
    g = Governor(6, readers=fake(), ttl_s=0)
    assert g.thermal_max_c == gv.DEFAULT_THERMAL_MAX_C and g.floor == 1


def test_readers_that_raise_degrade_to_max_parallel():
    g = gov(readers={"load": boom, "mem_pct": boom, "disk_free_pct": boom, "temp_c": boom})
    assert g.target() == 8
    assert g.last_reason == "no readable sensors"
    st = g.state()
    assert set(st["health"]["errors"]) == {"load", "mem_pct", "disk_free_pct", "temp_c"}
    assert st["factors"] == {}


def test_partial_sensor_failure_uses_the_rest():
    r = fake(temp=95)
    r["load"] = boom
    r["mem_pct"] = lambda: float("nan")
    r["disk_free_pct"] = lambda: None
    g = gov(readers=r)
    assert g.target() == 1
    h = g.read()
    assert h.load is None and h.mem_pct is None and h.disk_free_pct is None and h.temp_c == 95
    assert "load" in h.errors and "mem_pct" in h.errors


def test_target_never_zero_and_respects_want():
    g = gov(max_parallel=0, readers=fake(temp=200))
    assert g.max_parallel == 1 and g.target() == 1
    g = gov(readers=fake())
    assert g.target(3) == 3 and g.target(0) == 1


def test_disabled_governor_and_internal_error_return_max(monkeypatch):
    assert gov(readers=fake(temp=200), enabled=False).target() == 8
    g = gov(readers=fake(temp=200))
    monkeypatch.setattr(g, "factors", lambda h: 1 / 0)
    assert g.target() == 8 and g.last_reason.startswith("governor error")


def test_readings_are_cached_for_ttl():
    calls = []
    clock = [100.0]
    r = fake()
    r["load"] = lambda: calls.append(1) or 0.1
    g = Governor(4, readers=r, ttl_s=5, clock=lambda: clock[0])
    g.target(); g.target()
    assert len(calls) == 1
    clock[0] += 6
    g.target()
    assert len(calls) == 2
    g.read(fresh=True)
    assert len(calls) == 3


def test_from_settings_and_env_switch(monkeypatch, tmp_path):
    from types import SimpleNamespace
    g = Governor.from_settings(SimpleNamespace(max_parallel=5, governor=True, min_parallel=2,
                                               thermal_max_c=70, data_dir=tmp_path), readers=fake())
    assert (g.max_parallel, g.floor, g.thermal_max_c, g.enabled) == (5, 2, 70, True)
    monkeypatch.setenv("AVRONE_GOVERNOR", "off")
    assert Governor.from_settings(SimpleNamespace(max_parallel=3)).enabled is False
    monkeypatch.setenv("AVRONE_GOVERNOR", "on")
    assert Governor.from_settings(SimpleNamespace()).enabled is True
    monkeypatch.setenv("AVRONE_GOVERNOR", "0")
    monkeypatch.setenv("AVRONE_MIN_PARALLEL", "2")
    monkeypatch.setenv("AVRONE_THERMAL_MAX_C", "77")
    s = Settings()
    assert (s.governor, s.min_parallel, s.thermal_max_c) == (False, 2, 77)


# ------------------------------------------------------------------ default readers
def test_default_readers_never_raise_through_governor(tmp_path):
    g = Governor(4, data_dir=str(tmp_path / "missing" / "dir"), ttl_s=0)
    h = g.read()
    assert isinstance(h, NodeHealth)
    assert 1 <= g.target() <= 4


def test_sysfs_temp_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    for i, v in enumerate(["45000", "71500", "garbage", "-273000"]):
        d = tmp_path / f"thermal_zone{i}"
        d.mkdir()
        (d / "temp").write_text(v)
    assert gv.read_temp_c(str(tmp_path / "thermal_zone*" / "temp")) == 71.5
    assert gv.read_temp_c(str(tmp_path / "nothing*")) is None


def test_psutil_readers_when_available(monkeypatch):
    from types import SimpleNamespace
    ps = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(percent=42.0),
                         sensors_temperatures=lambda: {"coretemp": [SimpleNamespace(current=66.0),
                                                                    SimpleNamespace(current=None)]})
    monkeypatch.setattr(gv, "_psutil", lambda: ps)
    assert gv.read_mem_pct() == 42.0
    assert gv.read_temp_c("/nonexistent*") == 66.0
    ps.sensors_temperatures = boom  # psutil raising falls through to sysfs (none here)
    assert gv.read_temp_c("/nonexistent*") is None


def test_proc_meminfo_reader(monkeypatch, tmp_path):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    v = gv.read_mem_pct()
    assert v is None or 0 <= v <= 100
    assert gv.make_disk_reader(str(tmp_path))() > 0


# ------------------------------------------------------------------ wired into the squad
class TrackLLM:
    """Plans N workers; each worker call records the live worker count."""

    def __init__(self, n):
        self.n = n
        self.live = 0
        self.peak = 0

    async def chat(self, messages, **kw):
        sys = messages[0]["content"]
        if "PLANNER" in sys:
            return json.dumps({"subtasks": [{"role": f"W{i}", "focus": f"f{i}"} for i in range(self.n)]})
        if kw.get("lead"):
            return "LEAD " + "x" * 400
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.02)
        self.live -= 1
        return '{"final": "ok"}'


def _core(tmp_path, llm, readers, **over):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_parallel = 6
    s.governor = True
    for k, v in over.items():
        setattr(s, k, v)
    core = TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir))
    core.governor = Governor(s.max_parallel, min_parallel=1, thermal_max_c=85, readers=readers, ttl_s=0)
    return core


def _events(core, run_id):
    return [json.loads(l) for l in (core.codex.log_dir / f"{run_id}.jsonl").read_text().splitlines()]


def test_squad_on_hot_node_runs_one_worker_at_a_time(tmp_path):
    llm = TrackLLM(4)
    core = _core(tmp_path, llm, fake(temp=95))
    out = asyncio.run(core.squad("task", n=4, run_id="hot"))
    assert [a["result"] for a in out["agents"]] == ["ok"] * 4
    assert llm.peak == 1
    ev = [e for e in _events(core, "hot") if e["kind"] == "governor.throttle"]
    assert ev and ev[0]["data"]["parallel"] == 1 and ev[0]["data"]["workers"] == 4


def test_squad_on_cool_node_runs_all_workers_in_parallel(tmp_path):
    llm = TrackLLM(4)
    core = _core(tmp_path, llm, fake())
    out = asyncio.run(core.squad("task", n=4, run_id="cool"))
    assert len(out["agents"]) == 4 and llm.peak == 4
    assert not [e for e in _events(core, "cool") if e["kind"] == "governor.throttle"]


def test_squad_survives_broken_governor(tmp_path):
    llm = TrackLLM(3)
    core = _core(tmp_path, llm, fake())
    core.governor = None  # .target() raises AttributeError -> full parallelism
    out = asyncio.run(core.squad("task", n=3, run_id="nogov"))
    assert [a["result"] for a in out["agents"]] == ["ok"] * 3 and llm.peak == 3


def test_deep_wave_is_governed_too(tmp_path):
    llm = TrackLLM(0)
    core = _core(tmp_path, llm, fake(load=5.0))
    out = asyncio.run(core.deep("q", first_answer="a", n=3, run_id="deep"))
    assert out["reviewers_ok"] == 3 and llm.peak == 1
