"""Round 5 governor edges: ramp math already covered; this covers registry, cache, concurrency, want, from_settings whitespace."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from avrone.governor import Governor, NodeHealth


def _readers(**vals):
    defaults = {"load": 0.1, "mem_pct": 20.0, "disk_free_pct": 50.0, "temp_c": 40.0}
    defaults.update(vals)
    return {k: (lambda v=v: v) for k, v in defaults.items()}


def test_from_settings_strips_governor_env(monkeypatch):
    monkeypatch.setenv("AVRONE_GOVERNOR", " off ")
    g = Governor.from_settings(SimpleNamespace(max_parallel=4), readers=_readers())
    assert g.enabled is False
    monkeypatch.setenv("AVRONE_GOVERNOR", "\tON\n")
    g = Governor.from_settings(SimpleNamespace(max_parallel=4), readers=_readers())
    assert g.enabled is True


def test_target_junk_want_does_not_raise():
    g = Governor(8, readers=_readers(), ttl_s=0)
    assert isinstance(g.target(want="nope"), int)
    assert 1 <= g.target(want="nope") <= 8
    assert 1 <= g.target(want=float("inf")) <= 8
    assert 1 <= g.target(want=float("nan")) <= 8
    assert 1 <= g.target(want=None) <= 8
    assert g.target(want=2) == 2


def test_repeated_targets_increment_throttle_and_honor_cache():
    clock = [0.0]
    calls = []

    def load():
        calls.append(clock[0])
        return 2.0  # at LOAD_HARD: fully throttled

    r = _readers(load=2.0)
    r["load"] = load
    g = Governor(8, min_parallel=1, readers=r, ttl_s=2, clock=lambda: clock[0])
    assert g.target() == 1
    assert g.throttle_count == 1
    g.target()  # cached, still throttled
    assert len(calls) == 1
    assert g.throttle_count == 2
    clock[0] += 3  # expire TTL
    # flip the sensor to healthy and re-read
    g.readers["load"] = lambda: (calls.append(clock[0]) or 0.1)
    assert g.target() == 8
    assert len(calls) == 2
    assert "healthy" in g.last_reason or g.last_target == 8


def test_custom_sensor_registry_and_replacement():
    r = _readers()
    r["unused_extra"] = lambda: 123.0  # unknown key is stored, not used in factors
    g = Governor(4, readers=r, ttl_s=0)
    h = g.read(fresh=True)
    assert getattr(h, "unused_extra", None) == 123.0
    assert "unused_extra" not in g.factors(h)
    # replacing mem_pct mid-life
    g.readers["mem_pct"] = lambda: 99.0
    assert g.target() == 1  # MEM_HARD is 95
    st = g.state()
    assert st["throttle_count"] >= 1
    assert "mem_pct" in st["factors"]
    assert 0.0 <= st["factors"]["mem_pct"] <= 1.0


def test_state_and_is_hot_with_disabled_governor():
    g = Governor(6, enabled=False, readers=_readers(temp_c=99.0), thermal_max_c=85, ttl_s=0)
    assert g.target() == 6 and g.last_reason == "governor off"
    assert g.is_hot() is True  # still reads sensors when throttling is off
    st = g.state()
    assert st["enabled"] is False and st["max_parallel"] == 6
    assert st["health"]["temp_c"] == pytest.approx(99.0)


def test_node_health_as_dict_rounds_and_keeps_none():
    h = NodeHealth(load=1.23456, mem_pct=None, disk_free_pct=10.0, temp_c=40.119, ts=1.0)
    d = h.as_dict()
    assert d["load"] == 1.23 and d["mem_pct"] is None and d["temp_c"] == 40.12


def test_concurrent_target_calls_do_not_crash():
    box = {"temp": 40.0}

    def temp():
        return box["temp"]

    g = Governor(8, readers=_readers(temp_c=40.0) | {"temp_c": temp}, ttl_s=0)
    errors = []

    def worker(n):
        try:
            box["temp"] = 40.0 + (n % 3) * 30  # cool / warm / hot
            t = g.target(want=n % 5)
            assert 1 <= t <= 8
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert 1 <= g.last_target <= 8
    st = g.state()
    assert isinstance(st["throttle_count"], int)
