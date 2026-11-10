"""Round 3 governor edges: overflow meminfo, mixed locale separators, disk percent out of range."""
from __future__ import annotations

import io
from types import SimpleNamespace

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


def test_read_mem_pct_thousands_and_decimal_together_is_unparsable(monkeypatch):
    """'1,000.5' combines a thousands comma with a decimal; float() must not crash, reading is None."""
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1,000.5 kB\nMemAvailable: 250.25 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_scientific_overflow_is_none(monkeypatch):
    """1e400 is inf; a non-finite MemTotal must not be treated as 100% used."""
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: 1e400 kB\nMemAvailable: 500 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_uint64_boundary_stays_in_range(monkeypatch):
    """2**64-sized kB figures must either parse to a 0..100 percent or be rejected, never crash."""
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo",
        "MemTotal: 18446744073709551616 kB\nMemAvailable: 9223372036854775808 kB\n"))
    got = gv.read_mem_pct()
    assert got is None or (0.0 <= got <= 100.0)


def test_read_mem_pct_nan_token_is_none(monkeypatch):
    _no_psutil_macos(monkeypatch)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: nan kB\nMemAvailable: 100 kB\n"))
    assert gv.read_mem_pct() is None


def test_disk_reader_free_exceeds_total_returns_none(tmp_path, monkeypatch):
    reader = gv.make_disk_reader(str(tmp_path))
    monkeypatch.setattr(gv.shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=1000, used=0, free=2000))
    assert reader() is None


def test_disk_reader_negative_free_returns_none(tmp_path, monkeypatch):
    reader = gv.make_disk_reader(str(tmp_path))
    monkeypatch.setattr(gv.shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=1000, used=2000, free=-5))
    assert reader() is None


def test_disk_reader_uint64_totals_stay_in_range(tmp_path, monkeypatch):
    reader = gv.make_disk_reader(str(tmp_path))
    monkeypatch.setattr(gv.shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=2**64, used=2**63, free=2**63))
    got = reader()
    assert got is None or (0.0 <= got <= 100.0)


def test_target_survives_overflow_readers():
    """Governor.target() must stay in [1, max_parallel] even when every sensor is overflowing."""
    g = gv.Governor(max_parallel=8, ttl_s=0, readers={
        "load": lambda: float("inf"),
        "mem_pct": lambda: float("nan"),
        "disk_free_pct": lambda: 1e400,
        "temp_c": lambda: float("-inf"),
    })
    n = g.target()
    assert isinstance(n, int)
    assert 1 <= n <= 8
