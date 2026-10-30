"""Tests for the local squad's fresh accepted-proposal gate."""
import importlib.util
import math
from pathlib import Path

import pytest


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "count_fresh_evolve_proposals.py"
SPEC = importlib.util.spec_from_file_location("count_fresh_evolve_proposals", HELPER)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def proposal(pid, ts, *, branch="evolve/test", files=None):
    return {"pid": pid, "branch": branch, "files": files if files is not None else ["tests/test_new.py"], "ts": ts}


def test_counts_only_unique_structurally_valid_proposals_from_this_run():
    rows = [
        proposal("fresh-a", 101.0),
        proposal("fresh-a", 102.0),  # duplicate metadata must not count twice
        proposal("fresh-b", 103),
        proposal("old", 99.99),
        proposal("empty-branch", 104, branch=""),
        proposal("empty-files", 104, files=[]),
        {"pid": "no-time", "branch": "evolve/no-time", "files": ["x.py"]},
        "not-an-object",
        None,
    ]
    assert MODULE.count_fresh_accepted(rows, 100.0) == 2


@pytest.mark.parametrize("timestamp", [math.nan, math.inf, -math.inf, True, "101", 10**1000])
def test_nonfinite_or_invalid_timestamps_never_count(timestamp):
    assert MODULE.count_fresh_accepted([proposal("bad", timestamp)], 100.0) == 0


def test_invalid_start_timestamp_and_report_shape_fail_closed():
    with pytest.raises(ValueError, match="finite"):
        MODULE.count_fresh_accepted([], math.inf)
    with pytest.raises(ValueError, match="JSON list"):
        MODULE.count_fresh_accepted({"pid": "not-a-list"}, 100.0)
