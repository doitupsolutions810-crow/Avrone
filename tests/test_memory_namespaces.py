"""Durable per-agent memory namespaces: isolation, reload from disk, caps, concurrent saves."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from avrone import memstore
from avrone import tools_extra as tx
from avrone.config import Settings
from avrone.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AVRONE_MEMORY_MAX_ENTRIES", "AVRONE_MEMORY_MAX_VALUE", "AVRONE_MEMORY_MAX_NAMESPACES"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


def agent(data, name, **kw):
    s = Settings(workspace=data.parent / "ws", data_dir=data)
    core = SimpleNamespace(settings=s, codex=None)
    return SimpleNamespace(core=core, run_id="r1", name=name, depth=0, **kw)


def call(ctx, tool, **args):
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    return run(reg.call(ctx, tool, args))


def test_agent_namespace_is_stable_and_local():
    assert memstore.agent_namespace("Builder-3") == "builder"
    assert memstore.agent_namespace("Builder-17") == "builder"
    assert memstore.agent_namespace("Fact-Checker-2") == "fact-checker"
    assert memstore.agent_namespace("") == "agent"
    assert memstore.agent_namespace("???") == "agent"
    assert memstore.is_local("builder") and not memstore.is_local("fleet:edge/builder")


def test_namespaces_isolate(data):
    a, b = agent(data, "Builder-1"), agent(data, "Researcher-2")
    call(a, "memory_save", key="db", value="postgres")
    call(b, "memory_save", key="db", value="sqlite")
    assert "postgres" in call(a, "memory_recall", query="db") and "sqlite" not in call(a, "memory_recall", query="db")
    assert "sqlite" in call(b, "memory_recall", query="db") and "postgres" not in call(b, "memory_recall", query="db")
    call(b, "memory_save", key="only_b", value="x")
    assert "only_b" not in call(a, "memory_list") and "only_b" in call(b, "memory_list")
    assert call(agent(data, "Stranger-1"), "memory_recall") == "No memories saved yet."
    # a pinned namespace wins, but only if it is a local one (an agent can't pin into fleet:...)
    assert "postgres" in call(agent(data, "X-1", memory_namespace="builder"), "memory_recall")
    assert call(agent(data, "X-1", memory_namespace="fleet:edge/builder"), "memory_recall") == "No memories saved yet."


def test_namespaces_survive_reload(data):
    call(agent(data, "Builder-1"), "memory_save", key="plan", value="ship v2 friday")
    call(agent(data, "Builder-1"), "memory_save", key="plan", value="ship v2 monday")
    # "restart": brand-new store/context objects, and the per-process counter differs
    fresh = agent(data, "Builder-9")
    out = call(fresh, "memory_recall", query="plan")
    assert "monday" in out and "friday" not in out
    assert "namespace builder: 1/" in call(fresh, "memory_list")
    spaces = memstore.MemoryStore(data / "memory.jsonl").namespaces()
    assert set(spaces) == {"builder"} and spaces["builder"]["plan"]["agent"] == "Builder-1"


def test_legacy_records_load_into_agent_namespace(data):
    data.mkdir(parents=True)
    (data / "memory.jsonl").write_text(json.dumps({"key": "old", "value": "v", "ts": 1, "agent": "Worker-1"}) + "\n")
    assert "old" in call(agent(data, "Worker-4"), "memory_recall")
    assert call(agent(data, "Other-1"), "memory_recall") == "No memories saved yet."


def test_entry_cap_evicts_oldest_atomically(data, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "3")
    a = agent(data, "Builder-1")
    for i in range(5):
        call(a, "memory_save", key=f"k{i}", value=str(i))
    keys = set(memstore.MemoryStore(data / "memory.jsonl").load("builder"))
    assert keys == {"k2", "k3", "k4"}
    call(a, "memory_save", key="k4", value="updated")  # overwriting an existing key never evicts
    assert set(memstore.MemoryStore(data / "memory.jsonl").load("builder")) == {"k2", "k3", "k4"}
    # the cap is per namespace: another agent is unaffected
    call(agent(data, "Other-1"), "memory_save", key="z", value="1")
    assert len(memstore.MemoryStore(data / "memory.jsonl").load("builder")) == 3
    assert not list(data.glob(".memory.jsonl.*.tmp"))  # temp files from the atomic rewrite are gone


def test_value_cap_refuses(data, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_VALUE", "10")
    a = agent(data, "Builder-1")
    assert call(a, "memory_save", key="ok", value="0123456789") == "saved memory 'ok'"
    out = call(a, "memory_save", key="big", value="x" * 11)
    assert out.startswith("ERROR") and "cap is 10" in out
    assert "big" not in call(a, "memory_list")
    assert call(a, "memory_save", key="k" * 201, value="v").startswith("ERROR")


def test_env_knobs_are_clamped_to_hard_caps(monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "999999999")
    monkeypatch.setenv("AVRONE_MEMORY_MAX_VALUE", "999999999")
    monkeypatch.setenv("AVRONE_MEMORY_MAX_NAMESPACES", "999999999")
    assert memstore.max_entries() == memstore.HARD_MAX_ENTRIES
    assert memstore.max_value() == memstore.HARD_MAX_VALUE
    assert memstore.max_namespaces() == memstore.HARD_MAX_NAMESPACES
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "0")
    monkeypatch.setenv("AVRONE_MEMORY_MAX_VALUE", "junk")
    assert memstore.max_entries() == 1 and memstore.max_value() == memstore.DEFAULT_MAX_VALUE


def test_namespace_cap(data, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_NAMESPACES", "2")
    call(agent(data, "A-1"), "memory_save", key="k", value="v")
    call(agent(data, "B-1"), "memory_save", key="k", value="v")
    out = call(agent(data, "C-1"), "memory_save", key="k", value="v")
    assert out.startswith("ERROR") and "namespace cap" in out
    assert call(agent(data, "A-1"), "memory_save", key="k2", value="v") == "saved memory 'k2'"


def test_concurrent_saves_do_not_corrupt(data):
    store_path = data / "memory.jsonl"
    data.mkdir(parents=True)

    def worker(n):
        st = memstore.MemoryStore(store_path)
        for i in range(40):
            st.save(f"agent{n}", f"k{i}", "v" * 50 + str(i))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for line in store_path.read_text().splitlines():
        json.loads(line)  # every line intact
    spaces = memstore.MemoryStore(store_path).namespaces()
    assert {f"agent{n}" for n in range(6)} == set(spaces)
    assert all(len(spaces[f"agent{n}"]) == 40 for n in range(6))


def test_concurrent_async_tool_saves(data):
    ctxs = [agent(data, f"Role{n}-1") for n in range(4)]
    reg = ToolRegistry()
    tx.register_extra_tools(reg)

    async def go():
        await asyncio.gather(*(reg.call(c, "memory_save", {"key": f"k{i}", "value": "v"})
                               for c in ctxs for i in range(10)))
    run(go())
    spaces = memstore.MemoryStore(data / "memory.jsonl").namespaces()
    assert all(len(spaces[f"role{n}"]) == 10 for n in range(4))


def test_compaction_keeps_latest(data):
    st = memstore.MemoryStore(data / "memory.jsonl")
    for i in range(300):
        st.save("builder", "same", f"v{i}")
    lines = (data / "memory.jsonl").read_text().splitlines()
    assert len(lines) < 300  # superseded records were compacted away
    assert st.load("builder")["same"]["value"] == "v299"



def test_reload_skips_corrupt_rows_and_normalizes_timestamps(data):
    data.mkdir(parents=True, exist_ok=True)
    path = data / "memory.jsonl"
    good = {"ns": "builder", "key": "kept", "value": "value", "ts": "not-a-number", "agent": "Builder-1"}
    path.write_text("{broken json\n[]\nnull\n" + json.dumps(good) + "\n")

    st = memstore.MemoryStore(path)
    row = st.load("builder")["kept"]
    assert row["value"] == "value" and row["ts"] == 0.0
    st.save("builder", "next", "works")
    assert st.load("builder")["next"]["value"] == "works"


def test_append_after_torn_last_record_keeps_next_record_separate(data):
    data.mkdir(parents=True, exist_ok=True)
    path = data / "memory.jsonl"
    path.write_bytes(b'{"ns":"builder","key":"torn"')

    st = memstore.MemoryStore(path)
    st.save("builder", "recovered", "readable")
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == '{"ns":"builder","key":"torn"'
    assert json.loads(lines[1])["key"] == "recovered"
    assert st.load("builder")["recovered"]["value"] == "readable"


def test_merge_rejects_non_object_and_non_finite_rows_without_aborting_batch(data):
    data.mkdir(parents=True, exist_ok=True)
    st = memstore.MemoryStore(data / "memory.jsonl")
    ns = "fleet:edge/researcher"
    stats = st.merge(ns, [
        None,
        "not-an-object",
        42,
        {"key": "valid", "value": "kept", "ts": 10},
        {"key": "nan-ts", "value": "refuse", "ts": "nan"},
    ])
    assert stats == {"merged": 1, "skipped": 0, "rejected": 4}
    assert st.load(ns)["valid"]["value"] == "kept"
    assert "nan-ts" not in st.load(ns)
