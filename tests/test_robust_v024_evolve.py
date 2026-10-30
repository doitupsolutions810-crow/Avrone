"""v0.24 robustness: evolve proposal ids come from agents, so evolve_reset / evolve_diff / evolve_list
must never act outside data_dir/evolve, and must survive corrupt or partial proposal files."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import evolve


def run(c):
    return asyncio.run(c)


def mkctx(tmp):
    calls = []

    class Runner:
        workspace = str(tmp)

        async def run(self, cmd, timeout=None):
            calls.append(cmd)
            from avrone.sovereign import CommandResult
            return CommandResult(cmd, 0, "diff-out", "")
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp / "d")), runner=Runner(),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Evolver-1", depth=0), calls


def victim(tmp):
    v = tmp / "victim"
    v.mkdir()
    (v / "precious.txt").write_text("keep me")
    return v


def test_reset_pid_traversal_refused(tmp_path):
    ctx, _ = mkctx(tmp_path)
    v = victim(tmp_path)
    (tmp_path / "d").mkdir(exist_ok=True)
    (tmp_path / "d" / "evil.json").write_text(json.dumps({"worktree": str(v)}))
    out = run(evolve.t_evolve_reset(ctx, {"pid": "../evil"}))
    assert out.startswith("ERROR")
    assert (v / "precious.txt").exists() and (tmp_path / "d" / "evil.json").exists()


def test_reset_worktree_outside_evolve_dir_not_deleted(tmp_path):
    ctx, _ = mkctx(tmp_path)
    v = victim(tmp_path)
    d = evolve._data(ctx)
    (d / "p1.json").write_text(json.dumps({"pid": "p1", "worktree": str(v)}))
    out = run(evolve.t_evolve_reset(ctx, {"pid": "p1"}))
    assert (v / "precious.txt").exists()
    assert "discarded proposal p1" in out and not (d / "p1.json").exists()


def test_reset_own_worktree_removed(tmp_path):
    ctx, _ = mkctx(tmp_path)
    d = evolve._data(ctx)
    wt = d / "wt-p2"
    wt.mkdir()
    (wt / "f").write_text("x")
    (d / "p2.json").write_text(json.dumps({"pid": "p2", "worktree": str(wt)}))
    assert "discarded proposal p2" in run(evolve.t_evolve_reset(ctx, {"pid": "p2"}))
    assert not wt.exists()


@pytest.mark.parametrize("body", ["{not json", "[]", '{"pid": "p3"}', '{"worktree": 5}'])
def test_reset_and_diff_survive_corrupt_meta(tmp_path, body):
    ctx, calls = mkctx(tmp_path)
    d = evolve._data(ctx)
    (d / "p3.json").write_text(body)
    out = run(evolve.t_evolve_diff(ctx, {"pid": "p3"}))
    assert out.startswith("ERROR") and calls == []
    out = run(evolve.t_evolve_reset(ctx, {"pid": "p3"}))
    assert "discarded proposal p3" in out and not (d / "p3.json").exists()


def test_diff_pid_traversal_refused(tmp_path):
    ctx, calls = mkctx(tmp_path)
    (tmp_path / "d").mkdir(exist_ok=True)
    (tmp_path / "d" / "evil.json").write_text(json.dumps({"worktree": str(tmp_path)}))
    out = run(evolve.t_evolve_diff(ctx, {"pid": "../evil"}))
    assert out.startswith("ERROR") and calls == []


def test_diff_outside_worktree_refused(tmp_path):
    ctx, calls = mkctx(tmp_path)
    d = evolve._data(ctx)
    (d / "p4.json").write_text(json.dumps({"pid": "p4", "worktree": "/"}))
    assert run(evolve.t_evolve_diff(ctx, {"pid": "p4"})).startswith("ERROR") and calls == []


def test_list_skips_partial_and_foreign_json(tmp_path):
    ctx, _ = mkctx(tmp_path)
    d = evolve._data(ctx)
    (d / "good.json").write_text(json.dumps({"pid": "good", "passed": True, "goal": "g", "files": ["a.py"],
                                             "by": "w"}))
    (d / "partial.json").write_text(json.dumps({"pid": "partial"}))
    (d / "list.json").write_text("[1, 2]")
    (d / "bad.json").write_bytes(b"\xff\xfe")
    out = run(evolve.t_evolve_list(ctx, {}))
    assert "good [PASS]" in out
