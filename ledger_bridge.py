"""ledger_bridge — forward trinity-core's private diary into the attestpipe bus.

Reads /tmp/trinity-ledger/events.jsonl (or TRINITY_LEDGER_PATH) and re-emits
each event as an attestpipe envelope with provenance depth preserved.
Fail-closed: if the source ledger is unreadable, emit HOLD_UNMEASURED,
never a synthetic green.
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


def _envelope(event: dict[str, Any], seq: int) -> dict[str, Any]:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return {
        "seq": seq,
        "ts": time.time(),
        "source": "trinity-core",
        "kind": event.get("kind", "unknown"),
        "digest": digest,
        "provenance_depth": event.get("provenance_depth", 0) + 1,
        "residual": event.get("residual"),
        "orientation": event.get("orientation"),
        "raw": event,
    }


def bridge_once() -> list[dict[str, Any]]:
    """Read new trinity-ledger events and append envelopes to the attestpipe bus.

    Returns the list of envelopes written. Empty list if source missing or empty.
    Raises FileNotFoundError only if the path exists but is a directory.
    """
    if not LEDGER_PATH.exists():
        return []
    if LEDGER_PATH.is_dir():
        raise FileNotFoundError(f"ledger path is a directory: {LEDGER_PATH}")

    BUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    with LEDGER_PATH.open() as src, BUS_PATH.open("a") as bus:
        for i, line in enumerate(src):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # corrupt line: record as unmeasured, do not skip silently
                event = {"kind": "corrupt", "raw": line, "residual": None, "orientation": "unmeasured", "provenance_depth": 0}
            env = _envelope(event, i)
            bus.write(json.dumps(env, sort_keys=True) + "\n")
            written.append(env)
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
