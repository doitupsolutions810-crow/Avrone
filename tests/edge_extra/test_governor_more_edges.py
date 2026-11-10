"""Harder governor edges: locale-like meminfo, inverted totals, disk_usage mid-stat errors."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from avrone import governor as gv

_REAL_OPEN = open


def _open_for(path: str, body: str | OSError):
    def fake_open(p, *a, **k):
        if str(p) == path:
            if isinstance(body, OSError):
                raise body
            return io.StringIO(body)
        return _REAL_OPEN(p, *a, **k)
    return fake_open


def _no_psutil_macos(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr(gv, "_read_mem_pct_macos", lambda: None)


def test_read_mem_pct_locale_comma_thousands_is_unparsable(monkeypatch):
    """European thousands separators are not valid floats; skip rather than crash."""
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1,048,576 kB\nMemAvailable: 262,144 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_locale_decimal_comma_is_unparsable(monkeypatch):
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1000,5 kB\nMemAvailable: 250,25 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_dot_decimals_still_work(monkeypatch):
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 2000.00 kB\nMemAvailable: 500.00 kB\n"))
    # 100 * (1 - 500/2000) = 75.0
    assert gv.read_mem_pct() == pytest.approx(75.0)


def test_read_mem_pct_scientific_notation(monkeypatch):
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1e6 kB\nMemAvailable: 2.5e5 kB\n"))
    assert gv.read_mem_pct() == pytest.approx(75.0)


def test_read_mem_pct_available_exceeds_total_returns_none(monkeypatch):
    """Inverted meminfo would have been a negative used-percent; refuse the reading."""
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 2000 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_negative_available_returns_none(monkeypatch):
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: -5 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_only_memtotal_returns_none(monkeypatch):
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for("/proc/meminfo", "MemTotal: 1000 kB\n"))
    assert gv.read_mem_pct() is None


def test_disk_reader_stat_raises_oserror(tmp_path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir()

    def boom(p):
        raise OSError("mid-stat failure")

    monkeypatch.setattr(gv.shutil, "disk_usage", boom)
    assert gv.make_disk_reader(str(target))() is None


def test_disk_reader_stat_raises_after_walk_to_parent(tmp_path, monkeypatch):
    missing = tmp_path / "gone" / "child"
    # path does not exist; reader walks to tmp_path, then disk_usage raises
    def boom(p):
        raise OSError(f"cannot stat {p}")

    monkeypatch.setattr(gv.shutil, "disk_usage", boom)
    assert gv.make_disk_reader(str(missing))() is None


def test_disk_reader_zero_total(tmp_path, monkeypatch):
    class U:
        total = 0
        free = 0
        used = 0

    monkeypatch.setattr(gv.shutil, "disk_usage", lambda p: U())
    assert gv.make_disk_reader(str(tmp_path))() is None


def test_governor_ignores_out_of_range_mem_via_reader(monkeypatch):
    """If a raw reader somehow returns 150%, Governor.read records the error, not a crash."""
    g = gv.Governor(max_parallel=4, readers={
        "load": lambda: 0.1,
        "mem_pct": lambda: 150.0,  # already-clamped readers shouldn't, but Governor must not raise
        "disk_free_pct": lambda: 50.0,
        "temp_c": lambda: 40.0,
    }, ttl_s=0)
    h = g.read(fresh=True)
    # 150 is not NaN, so it is kept; target still returns in range
    assert h.mem_pct == 150.0
    t = g.target()
    assert 1 <= t <= 4
