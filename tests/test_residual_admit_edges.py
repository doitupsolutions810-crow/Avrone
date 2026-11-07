"""Edge checks for signed residual admission. Run: python tests/test_residual_admit_edges.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RESIDUAL_LOG_ONLY"] = "0"
os.environ["AVRONE_RESIDUAL_GATE"] = "1"

from avrone.residual_admit import admit, heat  # noqa: E402
from avrone.lead_admit import lead_decision  # noqa: E402


def main() -> int:
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(("OK" if cond else "FAIL"), name)
        fails += int(not cond)

    ck("all_pass", admit(failures=0, runs=200)["action"] == "lead.merge")
    stamped = admit(failures=200, runs=200, orientation="pass")
    ck("stamp_blocked", stamped["action"] == "lead.rejected" and stamped["orientation"] == "fail" and "orientation_conflict" in stamped["reasons"])
    ck("bool_count", "evidence_domain" in admit(failures=True, runs=1)["reasons"])
    ck("nan_bound", "nonfinite_bound" in admit(S=0.1, bound=float("nan"))["reasons"])
    ck("lead_wrap", lead_decision(failures=200, runs=200)["action"] == "lead.rejected")
    ck("heat_sym", all(abs(heat(i / 200) - heat(1 - i / 200)) < 1e-12 for i in range(201)))
    os.environ["RESIDUAL_LOG_ONLY"] = "1"
    ck("log_only", admit(failures=200, runs=200)["action"] == "lead.merge")
    print(f"{7 - fails}/7 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
