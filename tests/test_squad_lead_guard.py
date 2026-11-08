"""Run: python tests/test_squad_lead_guard.py"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RESIDUAL_LOG_ONLY"] = "0"
os.environ["AVRONE_RESIDUAL_GATE"] = "1"

from avrone.squad_lead_guard import guard_squad  # noqa: E402


def endless():
    while True:
        yield "ok"


def main() -> int:
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(("OK" if cond else "FAIL"), name)
        fails += int(not cond)

    t = time.monotonic()
    g = guard_squad(endless())
    ck("no_hang", time.monotonic() - t < 0.5 and g["S"] is None and "not_a_squad" in g["reasons"])
    ck("dict", guard_squad({"a": "[A failed: x]"})["orientation"] == "unmeasured")
    ck("none", guard_squad([None])["S"] == 0 and guard_squad([None])["measured"] is True)
    ck("text", guard_squad(["None"])["orientation"] == "pass")
    ck("empty", guard_squad(())["S"] is None)
    os.environ["RESIDUAL_LOG_ONLY"] = "1"
    ck("log_only", guard_squad(endless())["action"] == "lead.merge")
    print(f"{6 - fails}/6 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
