"""Round 11 coverage: evolve branches (candidate checkpoint caps, resume refusals incl. a loop file
replaced under us, symlinked worktree)."""
import json
from types import SimpleNamespace

import pytest

from avrone import evolve


def _ectx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r", name="E", depth=0)


def test_checkpoint_candidates_unserialisable_and_big(monkeypatch):
    assert evolve._checkpoint_candidates([[{"path": "a", "content": object()}]]) is None
    monkeypatch.setattr(evolve, "CHECKPOINT_MAX_BYTES", 5)
    assert evolve._checkpoint_candidates([[{"path": "a", "content": "xxxxxxxx"}]]) is None


def test_num_or():
    assert evolve._num_or(True, 3.0) == 3.0 and evolve._num_or("x", 2.0) == 2.0 and evolve._num_or("4", 0) == 4.0


@pytest.mark.parametrize("body", ["[1, 2]", "{torn"])
def test_resume_loop_file_replaced_under_us(tmp_path, monkeypatch, body):
    ctx = _ectx(tmp_path)
    p = evolve._loop_path(ctx, "L9")
    p.write_text(body)
    monkeypatch.setattr(evolve, "evolve_status", lambda c, lid=None: {"loop_id": "L9", "status": "interrupted"})
    r = evolve._resume_start(ctx, "L9", None, None, None, None, None)
    assert "unreadable" in r["error"] and r["loop_id"] is None


def test_resume_without_goal(tmp_path, monkeypatch):
    ctx = _ectx(tmp_path)
    evolve._loop_path(ctx, "L8").write_text(json.dumps({"loop_id": "L8", "iterations": [], "goal": "  "}))
    monkeypatch.setattr(evolve, "evolve_status", lambda c, lid=None: {"loop_id": "L8", "status": "interrupted"})
    cands = [[{"path": "a.py", "content": "x"}]]
    assert "has no goal" in evolve._resume_start(ctx, "L8", cands, None, None, None, None)["error"]


def test_proposal_symlinked_worktree_refused(tmp_path):
    ctx = _ectx(tmp_path)
    d = evolve._data(ctx)
    real = d / "wt-real"
    real.mkdir()
    (d / "wt-link").symlink_to(real)
    (d / "p.json").write_text(json.dumps({"pid": "p", "worktree": str(d / "wt-link")}))
    mp, meta, wt, err = evolve._proposal(ctx, "p")
    assert wt is None and "no worktree" in err
