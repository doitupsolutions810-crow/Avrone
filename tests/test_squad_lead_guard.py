"""Run: python tests/test_squad_lead_guard.py"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RESIDUAL_LOG_ONLY"] = "0"
os.environ["AVRONE_RESIDUAL_GATE"] = "1"

from avrone.squad_lead_guard import guard_squad  # noqa: E402


class Evil(list):
    def __len__(self):
        while True:
            pass


def main() -> int:
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(("OK" if cond else "FAIL"), name)
        fails += int(not cond)

    box = ["second", "third"]

    class Insert:
        def __str__(self):
            box.insert(0, "extra-fail")
            return ""

    box.insert(0, Insert())
    g = guard_squad(box)
    ck("insert", g["workers"] == 3 and g["worker_failures"] == 1 and g["orientation"] == "mixed")

    grown = []

    class Grow:
        def __str__(self):
            grown.append(Grow())
            return ""

    grown.append(Grow())
    t = time.monotonic()
    g = guard_squad(grown)
    ck("grow", time.monotonic() - t < 0.5 and g["workers"] == 1 and len(grown) == 2)
    ck("subclass", guard_squad(Evil(["ok"]))["S"] is None)
    ck("none", guard_squad([None])["S"] == 0 and guard_squad([None])["orientation"] == "fail")
    ck("text", guard_squad(["None"])["orientation"] == "pass")
    print(f"{5 - fails}/5 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
