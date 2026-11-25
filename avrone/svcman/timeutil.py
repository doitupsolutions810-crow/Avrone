"""UTC timestamps for the supervisor event log."""

from __future__ import annotations

from datetime import datetime, timezone


def iso_now() -> str:
    """Millisecond UTC timestamp with a Z suffix, for example 2026-11-21T03:04:05.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
