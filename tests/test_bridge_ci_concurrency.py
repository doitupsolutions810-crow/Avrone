"""Keep GitHub CI bounded to the newest branch/PR commit without hiding matrix failures."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_full_ci_cancels_obsolete_branch_or_pr_runs():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "\nconcurrency:\n" in source
    section = source.split("\nconcurrency:\n", 1)[1].split("\njobs:", 1)[0]
    assert "cancel-in-progress: true" in section
    assert "github.event.pull_request.number || github.ref" in section
    assert "github.workflow" in section


def test_full_ci_preserves_both_python_matrix_legs_and_autopilot_history():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert 'matrix: { python: ["3.11", "3.12"] }' in source
    assert "fail-fast: false" in source
    assert "fetch-depth: 0" in source
    assert "1121d26f02a6249f6e76f1dbe6230650183b316f" in source
