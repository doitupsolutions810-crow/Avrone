"""Read-only view of the built-in AVRONE supervisor.

The workspace calls the supervisor's loopback control API. A stopped supervisor
is reported as unreachable; it does not fail the workspace.
"""

from __future__ import annotations

import sys
from pathlib import Path


def probe() -> dict:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from avrone.svcman.client import probe_status
    except Exception as exc:
        return {"state": "unreachable", "detail": f"supervisor client unavailable: {exc}"}
    return probe_status()
