from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from avrone.evolve import (
    _daily_budget_snapshot,
    _finish_daily_budget,
    _reserve_daily_budget,
)


def _ctx(data_dir):
    return SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=data_dir)))


def test_daily_budget_reserves_worst_case_and_records_actual_time(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_SECONDS", "200")
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_PROPOSALS", "3")
    ctx = _ctx(tmp_path)

    first = _reserve_daily_budget(ctx, 100)
    assert _daily_budget_snapshot(ctx)["reserved_s"] == 100
    second = _reserve_daily_budget(ctx, 80)
    with pytest.raises(ValueError, match="time budget exhausted"):
        _reserve_daily_budget(ctx, 30)

    _finish_daily_budget(ctx, first, 12.5, True)
    _finish_daily_budget(ctx, second, 8.0, False)
    state = _daily_budget_snapshot(ctx)
    assert state["started"] == 2
    assert state["accepted"] == 1
    assert state["rejected"] == 1
    assert state["elapsed_s"] == 20.5
    assert state["reserved_s"] == 0


def test_daily_budget_caps_attempt_count_including_rejected_proposals(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_SECONDS", "1000")
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_PROPOSALS", "1")
    ctx = _ctx(tmp_path)
    reservation = _reserve_daily_budget(ctx, 10)
    _finish_daily_budget(ctx, reservation, 2, False)
    with pytest.raises(ValueError, match="daily proposal limit reached"):
        _reserve_daily_budget(ctx, 1)


def test_daily_budget_reservations_are_serialized_across_threads(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_SECONDS", "100")
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_PROPOSALS", "100")
    ctx = _ctx(tmp_path)

    def try_reserve(_):
        try:
            return _reserve_daily_budget(ctx, 10)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        reservations = list(pool.map(try_reserve, range(20)))
    admitted = [r for r in reservations if r is not None]
    assert len(admitted) == 10
    state = _daily_budget_snapshot(ctx)
    assert state["started"] == 10
    assert state["reserved_s"] == 100


def test_corrupt_daily_budget_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_DAILY_SECONDS", "100")
    ctx = _ctx(tmp_path)
    from avrone.evolve import _budget_day, _budget_paths
    budget_path, _ = _budget_paths(ctx, _budget_day())
    budget_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        _reserve_daily_budget(ctx, 1)


def test_daily_budget_rejects_nonfinite_and_inconsistent_state(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    from avrone.evolve import _budget_day, _budget_paths
    day = _budget_day()
    budget_path, _ = _budget_paths(ctx, day)
    budget_path.write_text(
        '{"day":"' + day + '","started":1,"accepted":0,"rejected":0,'
        '"elapsed_s":NaN,"reserved_s":0}',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="corrupt"):
        _reserve_daily_budget(ctx, 1)

    budget_path.write_text(
        '{"day":"' + day + '","started":-1,"accepted":0,"rejected":0,'
        '"elapsed_s":0,"reserved_s":0}',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="invalid counters"):
        _reserve_daily_budget(ctx, 1)


def test_daily_budget_refuses_symlinked_state(tmp_path):
    ctx = _ctx(tmp_path)
    from avrone.evolve import _budget_day, _budget_paths
    budget_path, _ = _budget_paths(ctx, _budget_day())
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    budget_path.symlink_to(outside)
    with pytest.raises(RuntimeError, match="must not be symlinks"):
        _reserve_daily_budget(ctx, 1)
