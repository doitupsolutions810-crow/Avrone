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

    box = []

    class Grow:
        def __str__(self):
            box.append(Grow())
            return ""

    box.append(Grow())
    t = time.monotonic()
    g = guard_squad(box)
    ck("grow", time.monotonic() - t < 0.5 and g["workers"] == 1 and g["worker_failures"] == 1 and len(box) == 2)

    box2 = ["keep", "drop", "drop"]

    class Shrink:
        def __str__(self):
            del box2[1:]
            return "ok"

    box2[0] = Shrink()
    g = guard_squad(box2)
    ck("shrink", g["workers"] == 3 and g["worker_failures"] == 2 and g["orientation"] == "mixed")
    ck("subclass", guard_squad(Evil(["ok"]))["S"] is None)
    ck("none", guard_squad([None])["orientation"] == "fail" and guard_squad([None])["S"] == 0)
    ck("text", guard_squad(["None"])["orientation"] == "pass")
    print(f"{5 - fails}/5 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
