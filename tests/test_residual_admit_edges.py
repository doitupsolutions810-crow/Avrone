"""Edge checks for signed residual admission. Run: python tests/test_residual_admit_edges.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RESIDUAL_LOG_ONLY"] = "0"
os.environ["AVRONE_RESIDUAL_GATE"] = "1"

from avrone.residual_admit import admit, heat  # noqa: E402


def main() -> int:
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(("OK" if cond else "FAIL"), name)
        fails += int(not cond)

    a = admit(failures=0, runs=200)
    ck("all_pass", a["action"] == "lead.merge" and a["S"] == 0 and a["orientation"] == "pass")
    b = admit(failures=200, runs=200)
    ck("all_fail", b["action"] == "lead.rejected" and b["S"] == 0 and "orientation_fail" in b["reasons"])
    c = admit(failures=100, runs=200)
    ck("mixed", c["accepted"] and abs(c["S"] - 0.25) < 1e-12)
    ck("nan_bound", admit(S=0.1, bound=float("nan"))["action"] == "lead.rejected")
    ck("inf_bound", admit(S=0.1, bound=float("inf"))["action"] == "lead.rejected")
    ck("domain", "evidence_domain" in admit(failures=3, runs=0)["reasons"])
    ck("heat_sym", all(abs(heat(i / 500) - heat(1 - i / 500)) < 1e-12 for i in range(501)))
    os.environ["RESIDUAL_LOG_ONLY"] = "1"
    d = admit(failures=200, runs=200)
    ck("log_only", d["action"] == "lead.merge" and d["accepted"] is False)
    print(f"{8 - fails}/8 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
