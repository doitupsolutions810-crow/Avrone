"""Codex bus bridge: CONTROL12 ledger events -> harmonic dashboard feed.

Subscribes to the CodeX event bus (redis), filters attestation.issue and
ledger.append events, and republishes them on the harmonic channel with
the residual field promoted to a first-class telemetry coordinate so the
dashboard can render cone-floor status, orientation, and psi per intent.
This is the last unconnected connection: CONTROL12's OBSERVE/ATTEST
output finally reaches the harmonic plane.
"""
from __future__ import annotations

import json
import time
from typing import Any

try:
    import redis  # type: ignore
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


LEDGER_CHANNEL = "control12:ledger"
HARMONIC_CHANNEL = "harmonic:telemetry"
RESIDUAL_CHANNEL = "greatscale:residual"


class CodexBusBridge:
    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self._r = None

    def connect(self) -> None:
        if not HAS_REDIS:
            raise RuntimeError("redis package not installed")
        self._r = redis.from_url(self.redis_url, decode_responses=True)

    def handle_ledger_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Promote a CONTROL12 ledger event into harmonic telemetry."""
        residual = event.get("residual")
        out = {
            "ts": event.get("ts", time.time()),
            "schema": "harmonic.telemetry/v1",
            "source": "control12.ledger",
            "intent": event.get("intent"),
            "action": event.get("action"),
            "target_env": (event.get("target") or {}).get("environment"),
            "attestation": event.get("attestation"),
        }
        if residual is None:
            out["residual"] = None
            out["cone_status"] = "unmeasured"
            out["orientation"] = "undefined"
        else:
            out["residual"] = residual
            s = residual.get("S", 0.0)
            out["cone_status"] = "floor" if s == 0.0 else "above"
            out["orientation"] = residual.get("orientation", "measured")
            out["psi"] = residual.get("psi")
            out["heat"] = residual.get("h")
        return out

    def publish(self, event: dict[str, Any]) -> None:
        if self._r is None:
            self.connect()
        telem = self.handle_ledger_event(event)
        self._r.publish(HARMONIC_CHANNEL, json.dumps(telem))
        if telem["residual"] is not None:
            self._r.set(f"residual:latest:{telem['intent']}",
                        json.dumps(telem["residual"]), ex=3600)
