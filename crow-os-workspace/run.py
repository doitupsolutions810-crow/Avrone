#!/usr/bin/env python3
"""Start Crow-OS AVRONE Workspace.

From the repository root:
    python crow-os-workspace/run.py

Then open http://127.0.0.1:8765
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CROW_HOME", str(ROOT))


def main() -> None:
    import uvicorn

    from crow_os.api import create_app

    host = os.environ.get("CROW_HOST", "127.0.0.1")
    port = int(os.environ.get("CROW_PORT", "8765"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
