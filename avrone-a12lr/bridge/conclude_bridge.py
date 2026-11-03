#!/usr/bin/env python3
"""Control704 bridge conclude output for AVRONE takeover."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
from grok_bot_integration import bridge_output, gate_ok  # type: ignore

def main() -> None:
    out = bridge_output()
    print(json.dumps(out, indent=2))
    if not gate_ok():
        print("\n# GATE: Set ALLOW_AGENT_EXECUTION=true to enable runtime start.", file=sys.stderr)

if __name__ == "__main__":
    main()
