"""Cross-process global blackboard admission and cursor order invariants."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from pathlib import Path
import time
from types import SimpleNamespace

from avrone import bridge as bm


def _core(root):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=Path(root)))


def _single_id_competitor(args):
    root, number = args
    root = Path(root)
    core = _core(root)
    (root / f"blackboard-ready-{number}").touch()
    deadline = time.monotonic() + 20
    while not all((root / f"blackboard-ready-{i}").exists() for i in range(2)):
        if time.monotonic() > deadline:
            raise RuntimeError("process start barrier timed out")
        time.sleep(0.01)
    return bm.blackboard_accept(core, {
        "id": "same-global-id", "run_id": f"run-{number}",
        "kind": "finding", "text": f"worker-{number}", "agent": "worker",
        "ts": number + 1,
    })


def test_two_processes_cannot_accept_same_id_into_different_runs(tmp_path):
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        results = list(pool.map(_single_id_competitor,
                                [(str(tmp_path), 0), (str(tmp_path), 1)]))
    assert sorted(value[0] for value in results) == [False, True]
    assert {value[1] for value in results} == {"same-global-id"}
    all_posts = bm.blackboard_posts(_core(tmp_path))
    assert len(all_posts) == 1
    assert all_posts[0]["id"] == "same-global-id"


def _unique_id_writer(args):
    root, number, total = args
    core = _core(root)
    for index in range(total):
        duplicate, _ = bm.blackboard_accept(core, {
            "id": f"writer-{number}-post-{index}", "run_id": f"run-{number}",
            "kind": "finding", "text": f"value-{number}-{index}",
            "agent": "worker", "ts": index + number * 100,
        })
        assert not duplicate
    return total


def test_parallel_processes_keep_every_distinct_global_cursor(tmp_path):
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=3, mp_context=context) as pool:
        submitted = list(pool.map(_unique_id_writer,
                                  [(str(tmp_path), number, 9) for number in range(3)]))
    assert submitted == [9, 9, 9]
    ids = [p["id"] for p in bm.blackboard_posts(_core(tmp_path))]
    assert len(ids) == 27
    assert len(set(ids)) == 27
    assert set(ids) == {
        f"writer-{number}-post-{index}"
        for number in range(3) for index in range(9)
    }


def test_global_lock_does_not_block_memory_only_blackboard():
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=None))
    with bm._blackboard_file_lock(core):
        assert bm.blackboard_posts(core) == []


def test_global_lock_refuses_symlink_sidecar_without_touching_target(tmp_path):
    root = tmp_path / "bridge"
    root.mkdir()
    outside = tmp_path / "foreign.lock"
    outside.write_text("keep", encoding="utf-8")
    (root / "_fanin_order.json.lock").symlink_to(outside)
    core = _core(tmp_path)
    with bm._blackboard_file_lock(core):
        pass
    assert outside.read_text(encoding="utf-8") == "keep"



def test_preopened_cached_bridge_detects_new_global_duplicate_from_another_core(tmp_path):
    """The global admission path must not inspect a stale cached Bridge."""
    old_core = _core(tmp_path)
    bm.for_core(old_core, "run-a")  # preopen and cache an empty run journal
    other_core = _core(tmp_path)
    original = {
        "id": "global-duplicate", "run_id": "run-a", "kind": "finding",
        "text": "accepted-by-other-core", "agent": "writer", "ts": 1,
    }
    first, _ = bm.blackboard_accept(other_core, original)
    assert not first

    same_id_in_different_run = dict(original, run_id="run-b", text="must-not-duplicate")
    second, _ = bm.blackboard_accept(old_core, same_id_in_different_run)
    assert second
    assert [p["text"] for p in bm.blackboard_posts(_core(tmp_path))] == [
        "accepted-by-other-core"
    ]


def test_cached_bridge_detects_external_tombstone_before_global_admission(tmp_path):
    """A tombstone appended by another writer must be visible in _id_state."""
    import json
    old_core = _core(tmp_path)
    cached = bm.for_core(old_core, "run-a")
    writer = bm.Bridge("run-a", tmp_path)
    with writer.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tombstones": ["tombstoned-external"]}) + "\n")

    duplicate, pid = bm.blackboard_accept(old_core, {
        "id": "tombstoned-external", "run_id": "run-b",
        "kind": "finding", "text": "resurrection-attempt",
        "agent": "writer", "ts": 1,
    })
    assert duplicate
    assert pid == "tombstoned-external"
    assert cached.get(pid) is None
    assert [p["id"] for p in bm.blackboard_posts(_core(tmp_path))] == []


def test_unmodified_cached_bridge_not_replayed_during_global_id_scan(tmp_path, monkeypatch):
    """Cross-Core safety should not introduce redundant full journal replays."""
    core = _core(tmp_path)
    cached = bm.for_core(core, "already-open")
    def fail_load():
        raise AssertionError("unchanged cached journal was unnecessarily replayed")
    monkeypatch.setattr(cached, "_load", fail_load)
    duplicate, pid = bm.blackboard_accept(core, {
        "id": "unique-entry", "run_id": "different-run",
        "kind": "finding", "text": "new", "agent": "writer", "ts": 1,
    })
    assert not duplicate
    assert pid == "unique-entry"
