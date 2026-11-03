"""Example AVRONE plugin: current date and time in any IANA timezone."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from avrone.tools import Tool


async def now(ctx, args: dict) -> str:
    tz_name = str(args.get("timezone") or "UTC").strip()
    try:
        tz = timezone.utc if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return f"ERROR: unknown IANA timezone '{tz_name}' (try e.g. America/New_York, Europe/London)"
    t = datetime.now(tz)
    return (f"{t.strftime('%A, %Y-%m-%d %H:%M:%S')} {t.tzname()} (UTC{t.strftime('%z')[:3]}:{t.strftime('%z')[3:]}) "
            f"in {tz_name}; ISO {t.isoformat(timespec='seconds')}")


def register(registry, settings) -> None:
    registry.register(Tool(
        "current_time", "get the current date and time in any IANA timezone (default UTC).",
        '{"timezone"?: str}', now, source="plugin:datetime_tool"))
