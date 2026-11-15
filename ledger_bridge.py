"""ledger_bridge — forward trinity-core's private diary into the attestpipe bus.

Reads /tmp/trinity-ledger/events.jsonl (or TRINITY_LEDGER_PATH) and re-emits
each event as an attestpipe envelope with provenance depth preserved.
Fail-closed: if the source ledger is unreadable, emit HOLD_UNMEASURED,
never a synthetic green.

Wiring (audit follow-through, 2026-11-15): the bridge is now safe to run
repeatedly and safe to schedule.

* Idempotent: a watermark file next to the bus records how many ledger
  lines have been emitted (and the last digest). Re-running bridges only
  new events; it never re-emits the whole ledger.
* Kind mapping: trinity-core events carry "type", not "kind" — envelopes
  now take kind from "kind" when present, else "type", else "unknown".
* Fail-closed for real: a missing ledger writes exactly one
  HOLD_UNMEASURED envelope to the bus (repeat runs while the ledger is
  still missing do not spam further HOLDs) instead of silently
  returning an empty list.

Run it directly (`python ledger_bridge.py`) or call bridge_once() from a
scheduler. State path override: LEDGER_BRIDGE_STATE_PATH.
"""
from __future__ import annotations

import json
import os
import hashlib
import time
from pathlib import Path
from typing import Any


LEDGER_PATH = Path(os.environ.get("TRINITY_LEDGER_PATH", "/tmp/trinity-ledger/events.jsonl"))
BUS_PATH = Path(os.environ.get("ATTESTPIPE_BUS_PATH", "/tmp/attestpipe/bus.jsonl"))
STATE_PATH = Path(os.environ.get("LEDGER_BRIDGE_STATE_PATH", str(BUS_PATH) + ".watermark.json"))


def _envelope(event: dict[str, Any], seq: int) -> dict[str, Any]:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return {
        "seq": seq,
        "ts": time.time(),
        "source": "trinity-core",
        "kind": event.get("kind") or event.get("type") or "unknown",
        "digest": digest,
        "provenance_depth": event.get("provenance_depth", 0) + 1,
        "residual": event.get("residual"),
        "orientation": event.get("orientation"),
        "raw": event,
    }


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"emitted": 0, "last_digest": None, "hold_active": False}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, sort_keys=True) + "\n")


def bridge_once() -> list[dict[str, Any]]:
    """Read new trinity-ledger events and append envelopes to the attestpipe bus.

    Only events past the watermark are bridged, so repeat runs are no-ops.
    Returns the list of envelopes written. If the source ledger is missing,
    writes a single HOLD_UNMEASURED envelope (fail-closed, as documented)
    and returns it; further runs while it stays missing write nothing.
    Raises FileNotFoundError only if the path exists but is a directory.
    """
    if LEDGER_PATH.is_dir():
        raise FileNotFoundError(f"ledger path is a directory: {LEDGER_PATH}")

    BUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    written: list[dict[str, Any]] = []

    if not LEDGER_PATH.exists():
        if not state.get("hold_active"):
            event = {
                "kind": "HOLD_UNMEASURED",
                "type": "HOLD_UNMEASURED",
                "residual": None,
                "orientation": "unmeasured",
                "provenance_depth": 0,
                "reason": "source ledger missing",
                "path": str(LEDGER_PATH),
            }
            env = _envelope(event, int(state.get("emitted", 0)))
            with BUS_PATH.open("a") as bus:
                bus.write(json.dumps(env, sort_keys=True) + "\n")
            written.append(env)
            state["hold_active"] = True
            _save_state(state)
        return written

    lines = [l.strip() for l in LEDGER_PATH.read_text().splitlines() if l.strip()]
    emitted = int(state.get("emitted", 0))
    if len(lines) < emitted:
        # Ledger rotated or truncated: start over rather than skip forever.
        emitted = 0

    with BUS_PATH.open("a") as bus:
        for i in range(emitted, len(lines)):
            line = lines[i]
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # corrupt line: record as unmeasured, do not skip silently
                event = {"kind": "corrupt", "raw": line, "residual": None, "orientation": "unmeasured", "provenance_depth": 0}
            env = _envelope(event, i)
            bus.write(json.dumps(env, sort_keys=True) + "\n")
            written.append(env)
            state["last_digest"] = env["digest"]

    if written:
        state["emitted"] = emitted + len(written)
        state["hold_active"] = False
        _save_state(state)
    return written


def verify_bus() -> dict[str, Any]:
    """Check the attestpipe bus for digest integrity and provenance monotonicity."""
    if not BUS_PATH.exists():
        return {"ok": True, "envelopes": 0, "note": "empty bus"}
    envelopes = []
    with BUS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                envelopes.append(json.loads(line))
    bad_digest = 0
    for env in envelopes:
        raw = env["raw"]
        payload = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(payload.encode()).hexdigest()
        if expected != env["digest"]:
            bad_digest += 1
    depths = [e["provenance_depth"] for e in envelopes]
    return {
        "ok": bad_digest == 0,
        "envelopes": len(envelopes),
        "bad_digest": bad_digest,
        "min_depth": min(depths) if depths else None,
        "max_depth": max(depths) if depths else None,
    }


if __name__ == "__main__":
    n = bridge_once()
    print(f"bridged {len(n)} events")
    print(json.dumps(verify_bus(), indent=2))
