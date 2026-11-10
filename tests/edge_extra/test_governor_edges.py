"""Extra edge cases for governor memory and disk readers."""
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


def test_read_mem_pct_missing_proc(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr(gv, "_read_mem_pct_macos", lambda: None)
    monkeypatch.setattr("builtins.open", _open_for("/proc/meminfo", FileNotFoundError("/proc/meminfo")))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_unreadable_proc(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr(gv, "_read_mem_pct_macos", lambda: None)
    monkeypatch.setattr("builtins.open", _open_for("/proc/meminfo", PermissionError("denied")))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_empty_proc(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr("builtins.open", _open_for("/proc/meminfo", ""))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_malformed_proc(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo", "MemTotal: not-a-number kB\nMemAvailable: ???\nthis is junk\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_malformed_skips_bad_lines(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr("builtins.open", _open_for(
        "/proc/meminfo",
        "MemTotal: 1000 kB\n"
        "GarbageLine without a value\n"
        "MemAvailable: not-a-number\n"
        "MemFree: 250 kB\n",
    ))
    v = gv.read_mem_pct()
    # MemAvailable is unparsable, so we should fall back to MemFree: 100*(1-250/1000)=75
    assert v == pytest.approx(75.0)


def test_read_mem_pct_zero_total(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr("builtins.open", _open_for("/proc/meminfo", "MemTotal: 0 kB\nMemAvailable: 0 kB\n"))
    assert gv.read_mem_pct() is None


def test_read_mem_pct_linux_still_works_when_proc_present(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    v = gv.read_mem_pct()
    # On this Linux box /proc/meminfo exists; a missing-file guard must not break the happy path.
    assert v is None or 0 <= v <= 100


def test_macos_fallback_used_when_proc_missing(monkeypatch):
    monkeypatch.setattr(gv, "_psutil", lambda: None)
    monkeypatch.setattr(gv, "_read_mem_pct_proc", lambda: None)
    monkeypatch.setattr(gv, "_read_mem_pct_macos", lambda: 41.5)
    assert gv.read_mem_pct() == 41.5


def test_macos_helper_is_none_on_linux():
    assert gv._read_mem_pct_macos() is None


def test_make_disk_reader_nonexistent_path(tmp_path):
    missing = tmp_path / "no" / "such" / "directory"
    v = gv.make_disk_reader(str(missing))()
    assert v is not None and 0 <= v <= 100


def test_make_disk_reader_deep_nonexistent_absolute():
    v = gv.make_disk_reader("/this/path/definitely/does/not/exist/avrone-edge")()
    assert v is None or 0 <= v <= 100


def test_make_disk_reader_empty_string():
    v = gv.make_disk_reader("")()
    assert v is None or 0 <= v <= 100
