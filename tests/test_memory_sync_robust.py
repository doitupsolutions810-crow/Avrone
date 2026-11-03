"""Round 32: memory sync and merge between fleet nodes. Covers how foreign keys and values are typed
(null, dict, list, bool, number, non-dict entries), how a full namespace takes in old entries, the
per-sender and total namespace quotas that keep the node's own agents able to save, and the
memory_sync tool's `keys` argument (a bare string, a number, junk)."""
import time

import pytest

from avrone import memstore
from avrone import tools_extra as tx
from avrone import fleet as fleet_mod
from tests.test_memory_sync import ENVS, mkfleet, ctx_for, store, transport_to, run

EXTRA_ENVS = ("AVRONE_MEMORY_MAX_REMOTE_NS",)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENVS + EXTRA_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def st(tmp_path):
    return memstore.MemoryStore(tmp_path / "memory.jsonl")


@pytest.fixture
def router(tmp_path, monkeypatch):
    return mkfleet(monkeypatch, tmp_path / "router", "router", allow="edge")


def payload(entries, sender="edge"):
    return {"from": sender, "hops": 1, "capability": "memory:sync", "entries": entries}


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize("v,exp", [("x", "x"), ("", ""), (5, "5"), (2.5, "2.5"), (None, None), (True, None),
                                   (False, None), ({"a": 1}, None), ([1], None), (float("nan"), None),
                                   (float("inf"), None)])
def test_merge_value(v, exp):
    assert memstore.merge_value(v) == exp


@pytest.mark.parametrize("v,exp", [("k", "k"), (" k ", "k"), (7, "7"), (True, None), (None, None),
                                   ({"a": 1}, None), (["k"], None), (1.5, None)])
def test_merge_key(v, exp):
    assert memstore.merge_key(v) == exp


@pytest.mark.parametrize("ns,exp", [("fleet:edge/builder", "edge"), ("builder", ""), ("fleet:x", ""),
                                    (None, ""), ("fleet:a/b/c", "a")])
def test_remote_sender(ns, exp):
    assert memstore.remote_sender(ns) == exp


def test_is_local_non_string():
    assert memstore.is_local(None) is False and memstore.is_local(5) is False


# ------------------------------------------------------------------ value / key typing in merge
@pytest.mark.parametrize("bad", [None, {"a": 1}, [1, 2], True])
def test_merge_refuses_non_text_value(st, bad):
    res = st.merge("ns", [{"key": "k", "value": bad, "ts": time.time()}])
    assert res == {"merged": 0, "skipped": 0, "rejected": 1}
    assert st.load("ns") == {}


def test_null_value_no_longer_stored_as_text_none(st):
    st.merge("ns", [{"key": "k", "value": None, "ts": 1}])
    assert "None" not in [r["value"] for r in st.load("ns").values()]


def test_merge_keeps_numbers_as_text(st):
    st.merge("ns", [{"key": "port", "value": 8080, "ts": 1}, {"key": "ratio", "value": 0.5, "ts": 1}])
    assert st.load("ns")["port"]["value"] == "8080" and st.load("ns")["ratio"]["value"] == "0.5"


@pytest.mark.parametrize("bad", [{"a": 1}, ["k"], True, None])
def test_merge_refuses_non_text_key(st, bad):
    assert st.merge("ns", [{"key": bad, "value": "v", "ts": 1}])["rejected"] == 1
    assert st.load("ns") == {}


def test_merge_integer_key(st):
    st.merge("ns", [{"key": 42, "value": "v", "ts": 1}])
    assert "42" in st.load("ns")


@pytest.mark.parametrize("junk", ["text", 5, None, ["k", "v"]])
def test_merge_non_dict_entry_rejected_not_crash(st, junk):
    res = st.merge("ns", [junk, {"key": "ok", "value": "v", "ts": 1}])
    assert res == {"merged": 1, "skipped": 0, "rejected": 1}


def test_missing_value_is_empty_text(st):
    st.merge("ns", [{"key": "k", "ts": 1}])
    assert st.load("ns")["k"]["value"] == ""


# ------------------------------------------------------------------ full namespace
def test_old_entry_into_full_namespace_is_skipped(st, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "3")
    st.merge("ns", [{"key": f"k{i}", "value": "v", "ts": 100 + i} for i in range(3)])
    res = st.merge("ns", [{"key": "ancient", "value": "v", "ts": 5}])
    assert res == {"merged": 0, "skipped": 1, "rejected": 0}
    assert set(st.load("ns")) == {"k0", "k1", "k2"}


def test_equal_to_oldest_into_full_namespace_is_skipped(st, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "2")
    st.merge("ns", [{"key": "a", "value": "v", "ts": 10}, {"key": "b", "value": "v", "ts": 20}])
    assert st.merge("ns", [{"key": "c", "value": "v", "ts": 10}])["skipped"] == 1


def test_newer_entry_into_full_namespace_evicts_oldest(st, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "3")
    st.merge("ns", [{"key": f"k{i}", "value": "v", "ts": 100 + i} for i in range(3)])
    assert st.merge("ns", [{"key": "new", "value": "v", "ts": 500}])["merged"] == 1
    assert set(st.load("ns")) == {"k1", "k2", "new"}


def test_batch_of_old_entries_does_not_churn_full_namespace(st, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "3")
    st.merge("ns", [{"key": f"k{i}", "value": "v", "ts": 100 + i} for i in range(3)])
    res = st.merge("ns", [{"key": f"old{i}", "value": "v", "ts": i} for i in range(10)])
    assert res["merged"] == 0 and res["skipped"] == 10
    assert set(st.load("ns")) == {"k0", "k1", "k2"}


def test_update_existing_key_in_full_namespace_still_works(st, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "2")
    st.merge("ns", [{"key": "a", "value": "v1", "ts": 10}, {"key": "b", "value": "v", "ts": 20}])
    assert st.merge("ns", [{"key": "a", "value": "v2", "ts": 30}])["merged"] == 1
    assert st.load("ns")["a"]["value"] == "v2"


def test_not_full_namespace_accepts_old_entry(st, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "5")
    st.merge("ns", [{"key": "a", "value": "v", "ts": 100}])
    assert st.merge("ns", [{"key": "old", "value": "v", "ts": 1}])["merged"] == 1


def test_corrupt_ts_in_store_does_not_crash_full_check(st, monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "2")
    st.path.write_text('{"ns":"ns","key":"a","value":"v","ts":"junk"}\n{"ns":"ns","key":"b","value":"v","ts":50}\n')
    res = st.merge("ns", [{"key": "c", "value": "v", "ts": 10}])
    assert res["merged"] == 1  # "junk" reads as 0, so c (10) is newer and evicts a


# ------------------------------------------------------------------ namespace quotas on receive
def test_sender_namespace_quota(router, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "4")
    res = router.receive_memory(payload([{"ns": f"n{i}", "key": "k", "value": "v", "ts": 1} for i in range(10)]))
    assert len(res["namespaces"]) == 4 and res["merged"] == 4
    assert res["rejected"] == 6 and res["over_quota"] == 6 and "AVRONE_MEMORY_MAX_REMOTE_NS" in res["note"]


def test_quota_counts_existing_namespaces(router, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "3")
    router.receive_memory(payload([{"ns": f"n{i}", "key": "k", "value": "v", "ts": 1} for i in range(3)]))
    res = router.receive_memory(payload([{"ns": "n9", "key": "k", "value": "v", "ts": 1},
                                         {"ns": "n0", "key": "k2", "value": "v", "ts": 1}]))
    assert res["over_quota"] == 1 and res["merged"] == 1  # existing n0 still updates


def test_quota_is_per_sender(router, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "2")
    router.receive_memory(payload([{"ns": f"n{i}", "key": "k", "value": "v", "ts": 1} for i in range(2)], "edge"))
    res = router.receive_memory(payload([{"ns": "n0", "key": "k", "value": "v", "ts": 1}], "edge2"))
    assert res["merged"] == 1 and "over_quota" not in res


def test_many_entries_in_one_namespace_not_quota_limited(router, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "1")
    res = router.receive_memory(payload([{"ns": "n", "key": f"k{i}", "value": "v", "ts": 1} for i in range(50)]))
    assert res["merged"] == 50 and "over_quota" not in res


def test_peers_cannot_lock_out_local_agents(router, monkeypatch):
    """The real bug: 200 one-entry namespaces from one child filled the store's 256 namespaces."""
    monkeypatch.setenv("AVRONE_MEMORY_MAX_NAMESPACES", "20")
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "4096")
    for s in ("edge", "edge2", "edge3"):
        router.receive_memory(payload([{"ns": f"n{i}", "key": "k", "value": "v", "ts": 1} for i in range(20)], s))
    spaces = store(router).namespaces()
    assert sum(1 for n in spaces if n.startswith("fleet:")) == 10  # half of 20
    for i in range(10):
        store(router).save(f"local{i}", "k", "v")  # the node's own agents still have room


def test_default_quota_is_32(router):
    res = router.receive_memory(payload([{"ns": f"n{i}", "key": "k", "value": "v", "ts": 1} for i in range(40)]))
    assert len(res["namespaces"]) == 32 and res["over_quota"] == 8


def test_quota_refusal_audited(router, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "1")
    router.receive_memory(payload([{"ns": "a", "key": "k", "value": "v", "ts": 1},
                                   {"ns": "b", "key": "k", "value": "v", "ts": 1}]))
    merges = [c for c in router.core.codex.emit.call_args_list if c.args[1] == "fleet.memory.merge"]
    assert merges and merges[-1].kwargs["over_quota"] == 1


def test_remote_ns_quota_env_clamped(monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "junk")
    assert memstore.remote_ns_per_sender() == 32
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "0")
    assert memstore.remote_ns_per_sender() == 1
    monkeypatch.setenv("AVRONE_MEMORY_MAX_REMOTE_NS", "999999")
    assert memstore.remote_ns_per_sender() == 4096


def test_receive_rejects_null_values(router):
    res = router.receive_memory(payload([{"ns": "b", "key": "k", "value": None, "ts": 1},
                                         {"ns": "b", "key": "k2", "value": "ok", "ts": 1}]))
    assert res["merged"] == 1 and res["rejected"] == 1


# ------------------------------------------------------------------ memory_sync keys argument
@pytest.fixture
def edge_pair(tmp_path, monkeypatch):
    edge = mkfleet(monkeypatch, tmp_path / "edge", "edge", parent="http://router")
    rt = mkfleet(monkeypatch, tmp_path / "router", "router", allow="edge")
    store(edge).save("builder", "db", "sqlite", agent="Builder-1")
    store(edge).save("builder", "d", "single-letter key", agent="Builder-1")
    store(edge).save("builder", "b", "another", agent="Builder-1")
    store(edge).save("builder", "7", "seven", agent="Builder-1")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to(rt))
    return edge, rt


def synced_keys(rt):
    return set(store(rt).load("fleet:edge/builder"))


def test_keys_bare_string_is_one_key(edge_pair):
    edge, rt = edge_pair
    out = run(tx.t_memory_sync(ctx_for(edge), {"keys": "db"}))
    assert out.startswith("synced 1 entries")
    assert synced_keys(rt) == {"db"}


def test_keys_number(edge_pair):
    edge, rt = edge_pair
    run(tx.t_memory_sync(ctx_for(edge), {"keys": 7}))
    assert synced_keys(rt) == {"7"}


def test_keys_list_still_works(edge_pair):
    edge, rt = edge_pair
    run(tx.t_memory_sync(ctx_for(edge), {"keys": ["db", " b "]}))
    assert synced_keys(rt) == {"db", "b"}


@pytest.mark.parametrize("bad", [{"a": 1}, True, [{"a": 1}], [None]])
def test_keys_junk_refused(edge_pair, bad):
    edge, rt = edge_pair
    assert run(tx.t_memory_sync(ctx_for(edge), {"keys": bad})).startswith("ERROR: keys must be")
    assert synced_keys(rt) == set()


@pytest.mark.parametrize("empty", [None, "", []])
def test_keys_empty_means_all(edge_pair, empty):
    edge, rt = edge_pair
    run(tx.t_memory_sync(ctx_for(edge), {"keys": empty}))
    assert synced_keys(rt) == {"db", "d", "b", "7"}
