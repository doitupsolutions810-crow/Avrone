"""Run: python tests/test_squad_lead_guard.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RESIDUAL_LOG_ONLY"] = "0"
os.environ["AVRONE_RESIDUAL_GATE"] = "1"

from avrone.squad_lead_guard import guard_squad, worker_failed  # noqa: E402


class Boom:
    def __str__(self):
        raise RuntimeError("no text")


def main() -> int:
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(("OK" if cond else "FAIL"), name)
        fails += int(not cond)

    ck("none", guard_squad([None, None])["action"] == "lead.rejected")
    ck("text_None", guard_squad(["None"])["orientation"] == "pass")
    ck("boom", guard_squad([Boom(), Boom()])["action"] == "lead.rejected")
    ck("boom_mixed", guard_squad([Boom(), "ok"])["orientation"] == "mixed")
    ck("str", "not_a_squad" in guard_squad("ok")["reasons"])
    ck("bytes", "not_a_squad" in guard_squad(b"ok")["reasons"])
    ck("gen", guard_squad(x for x in ["ok", None])["worker_failures"] == 1)
    ck("int", "not_a_squad" in guard_squad(3)["reasons"])
    ck("nul_kept", worker_failed("\x00") is False)
    os.environ["RESIDUAL_LOG_ONLY"] = "1"
    ck("log_only", guard_squad([Boom()])["action"] == "lead.merge")
    print(f"{10 - fails}/10 passed")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
