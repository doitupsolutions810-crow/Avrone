"""Tests for ledger_bridge wiring: idempotency, kind mapping, fail-closed HOLD.

Run: python -m pytest tests/test_ledger_bridge.py -q
"""
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fresh_bridge(tmp_path, monkeypatch):
    ledger = tmp_path / "events.jsonl"
    bus = tmp_path / "bus.jsonl"
    monkeypatch.setenv("TRINITY_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("ATTESTPIPE_BUS_PATH", str(bus))
    monkeypatch.setenv("LEDGER_BRIDGE_STATE_PATH", str(tmp_path / "state.json"))
    import ledger_bridge
    importlib.reload(ledger_bridge)
    return ledger_bridge, ledger, bus


def _write_ledger(ledger, events):
    ledger.write_text("".join(json.dumps(e) + "\n" for e in events))


def _bus_lines(bus):
    if not bus.exists():
        return []
    return [json.loads(l) for l in bus.read_text().splitlines() if l.strip()]


def test_bridge_is_idempotent(tmp_path, monkeypatch):
    lb, ledger, bus = _fresh_bridge(tmp_path, monkeypatch)
    _write_ledger(ledger, [{"type": "execution.completed"}, {"type": "execution.failed"}])
    first = lb.bridge_once()
    second = lb.bridge_once()
    assert len(first) == 2
    assert second == []
    assert len(_bus_lines(bus)) == 2


def test_only_new_events_are_bridged(tmp_path, monkeypatch):
    lb, ledger, bus = _fresh_bridge(tmp_path, monkeypatch)
    _write_ledger(ledger, [{"type": "a"}])
    lb.bridge_once()
    _write_ledger(ledger, [{"type": "a"}, {"type": "b"}, {"type": "c"}])
    new = lb.bridge_once()
    assert [e["kind"] for e in new] == ["b", "c"]
    assert len(_bus_lines(bus)) == 3


def test_kind_comes_from_type_field(tmp_path, monkeypatch):
    lb, ledger, bus = _fresh_bridge(tmp_path, monkeypatch)
    _write_ledger(ledger, [{"type": "execution.completed"}, {"kind": "explicit", "type": "other"}, {"note": "neither"}])
    envs = lb.bridge_once()
    assert [e["kind"] for e in envs] == ["execution.completed", "explicit", "unknown"]


def test_missing_ledger_emits_one_hold(tmp_path, monkeypatch):
    lb, ledger, bus = _fresh_bridge(tmp_path, monkeypatch)
    first = lb.bridge_once()
    second = lb.bridge_once()
    assert len(first) == 1 and first[0]["kind"] == "HOLD_UNMEASURED"
    assert first[0]["orientation"] == "unmeasured"
    assert second == []
    assert len(_bus_lines(bus)) == 1
    # ledger appears: real events bridge, and a later disappearance re-arms the HOLD
    _write_ledger(ledger, [{"type": "execution.completed"}])
    assert len(lb.bridge_once()) == 1
    ledger.unlink()
    third = lb.bridge_once()
    assert len(third) == 1 and third[0]["kind"] == "HOLD_UNMEASURED"


def test_corrupt_line_recorded_not_skipped(tmp_path, monkeypatch):
    lb, ledger, bus = _fresh_bridge(tmp_path, monkeypatch)
    ledger.write_text('{"type": "ok"}\nNOT JSON\n')
    envs = lb.bridge_once()
    assert [e["kind"] for e in envs] == ["ok", "corrupt"]
    assert lb.verify_bus()["ok"] is True
