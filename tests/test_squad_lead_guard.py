"""Run: python tests/test_squad_lead_guard.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RESIDUAL_LOG_ONLY"] = "0"
os.environ["AVRONE_RESIDUAL_GATE"] = "1"

from avrone.squad_lead_guard import guard_squad, worker_failed  # noqa: E402


def main() -> int:
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(("OK" if cond else "FAIL"), name)
        fails += int(not cond)

    ck("good", guard_squad(["found 3", "found 4"])["action"] == "lead.merge")
    silent = guard_squad(["", "   "])
    ck("silent", silent["action"] == "lead.rejected" and silent["S"] == 0 and silent["orientation"] == "fail")
    notes = guard_squad(["[Scout-1 failed: timeout]", "[Smith-2 failed: LLMError]"])
    ck("notes", notes["orientation"] == "fail" and notes["action"] == "lead.rejected")
    mixed = guard_squad(["verified", "[Scout-1 failed: timeout]"])
    ck("mixed", mixed["orientation"] == "mixed" and mixed["accepted"])
    rare = guard_squad(["ok"] * 199 + ["[A failed: x]"])
    ck("rare", rare["accepted"] and 0 < rare["S"] < 0.01)
    ck("bracket", worker_failed("[note] ok") is False)
    ck("empty_squad", "no_workers" in guard_squad([])["reasons"])
    os.environ["RESIDUAL_LOG_ONLY"] = "1"
    ck("log_only", guard_squad(["", ""])["action"] == "lead.merge")
    print(f"{8 - fails}/8 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
