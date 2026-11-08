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

    ck("none_objs", guard_squad([None, None])["action"] == "lead.rejected")
    ck("text_None", guard_squad(["None", "None"])["orientation"] == "pass")
    ck("none_mixed", guard_squad([None, "ok"])["orientation"] == "mixed")
    ck("bytes_not_none", worker_failed(b"") is False)
    ck("false_not_none", worker_failed(False) is False)
    ck("empty_list", worker_failed([]) is False)
    ck("blank", worker_failed("  ") is True)
    os.environ["RESIDUAL_LOG_ONLY"] = "1"
    ck("log_only", guard_squad([None])["action"] == "lead.merge")
    print(f"{8 - fails}/8 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
