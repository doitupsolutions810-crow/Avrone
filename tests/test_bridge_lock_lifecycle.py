"""Short-lived Bridge instances must not leak global lock-registry entries."""
from __future__ import annotations

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor

from avrone import bridge as bm


def test_same_persistent_journal_shares_lock_while_both_bridges_live(tmp_path):
    first = bm.Bridge("shared-live", tmp_path)
    second = bm.Bridge("shared-live", tmp_path)
    key = str(first.path.absolute())
    assert first._lock is second._lock
    assert bm._LOCKS[key] is first._lock


def test_lock_registry_releases_single_short_lived_bridge(tmp_path):
    bridge = bm.Bridge("short-lived", tmp_path)
    key = str(bridge.path.absolute())
    lock = weakref.ref(bridge._lock)
    assert key in bm._LOCKS
    del bridge
    gc.collect()
    assert key not in bm._LOCKS
    assert lock() is None


def test_shared_lock_is_retained_until_last_bridge_closes(tmp_path):
    first = bm.Bridge("shared-until-end", tmp_path)
    second = bm.Bridge("shared-until-end", tmp_path)
    key = str(first.path.absolute())
    lock = weakref.ref(first._lock)
    del first
    gc.collect()
    assert key in bm._LOCKS
    assert lock() is second._lock
    del second
    gc.collect()
    assert key not in bm._LOCKS
    assert lock() is None


def test_many_ephemeral_in_memory_bridges_leave_no_stale_lock_keys():
    keys = set()
    for index in range(128):
        bridge = bm.Bridge(f"ephemeral-{index}")
        key = f"mem:{id(bridge)}"
        keys.add(key)
        assert key in bm._LOCKS
        del bridge
    gc.collect()
    assert not keys.intersection(bm._LOCKS)


def test_simultaneous_bridge_initialization_shares_one_lock(tmp_path):
    with ThreadPoolExecutor(max_workers=12) as pool:
        bridges = list(pool.map(lambda _: bm.Bridge("concurrent-key", tmp_path), range(48)))
    assert len({id(bridge._lock) for bridge in bridges}) == 1
    key = str(bridges[0].path.absolute())
    assert bm._LOCKS[key] is bridges[0]._lock
