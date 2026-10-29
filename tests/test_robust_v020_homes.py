"""v0.20 robustness: agent homes, migration, agentfs quota and memstore edge cases (offline).

Pins the bugs fixed in this round:
  * pick_migration_target fell back to HOT peers when no cool one existed (thermal migration
    bounced agents between overheating nodes); junk health numbers / loads crashed the sort
  * concurrent migration of the same agent exported + posted twice (now refused while in flight)
  * agentpack.read_pack: duplicate manifest/memory members (last silently won), nested-JSON
    RecursionError, bool `created`, file-vs-dir path conflicts and over-long names (OSError -> 500),
    whole-stream decompression before the member/size caps
  * agentpack.import_pack: concurrent imports of one agent raced the swap / temp-dir name
  * agentfs quota_write: append followed a symlink planted after the checks; the temp file name
    was predictable and opened without O_EXCL/O_NOFOLLOW
  * memstore: one corrupt `ts` (string/NaN) made every later save raise TypeError; nested-JSON
    line raised RecursionError; merged NaN/inf ts won last-writer-wins forever
"""
import asyncio
import base64
import gzip
import io
import json
import os
import tarfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import agentfs, agentpack, memstore
from avrone import fleet as fleet_mod
from avrone.agentfs import AgentFSError
from avrone.agentfs_tools import dir_usage, quota_bytes, quota_write
from avrone.agentpack import PackError
from avrone.config import Settings
from avrone.fleet import FleetManager
from avrone.governor import Governor

ENVS = ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT", "AVRONE_FLEET_REQUIRE_ALLOW",
        "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_MAX_HOPS", "AVRONE_PUBLIC_URL",
        "AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_MIGRATE", "AVRONE_NODE_ID",
        "AVRONE_THERMAL_MAX_C", "AVRONE_MEMORY_MAX_ENTRIES", "AVRONE_MEMORY_MAX_VALUE")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


def mkfleet(mp, root, node, allow="", temp=None):
    mp.setenv("AVRONE_FLEET_TOKEN", "tok-0123456789abcdef")
    mp.setenv("AVRONE_NODE_ID", node)
    mp.setenv("AVRONE_FLEET_ALLOW", allow)
    core = MagicMock()
    core.active = {}
    core.codex.emit = MagicMock()
    core.settings = Settings(workspace=root / "ws", data_dir=root / "data")
    core.governor = Governor(4, thermal_max_c=80, readers={
        "temp_c": (lambda: temp[0]) if temp else (lambda: None), "load": lambda: None,
        "mem_pct": lambda: None, "disk_free_pct": lambda: None}, ttl_s=0)
    return FleetManager(core, root / "data")


def online(fm, nid, health=None, load=0):
    fm.nodes[nid] = {"node_id": nid, "url": f"http://{nid}", "last_seen": time.time(), "load": load,
                     **({"health": health} if health is not None else {})}


def seed(fm, aid="builder", text="hello", mtime=1_700_000_000):
    d = agentfs.agent_dir(fm.core.settings.workspace, aid)
    (d / "notes.txt").write_text(text)
    os.utime(d / "notes.txt", (mtime, mtime))
    return d


def events(fm, kind):
    return [c.kwargs for c in fm.core.codex.emit.call_args_list if c.args[1] == kind]


# ---------------------------------------------------------------- thermal migration targets
def test_no_hot_fallback_when_every_peer_is_hot(tmp_path, monkeypatch):
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    online(tx, "hot1", health={"temp_c": 95, "load": 0.1})
    online(tx, "hot2", health={"temp_c": 80, "load": 0.1})  # exactly at thermal max counts as hot
    with pytest.raises(RuntimeError, match="no online nodes to migrate to .*hot or throttled"):
        tx.pick_migration_target()


@pytest.mark.parametrize("state", ["hot", "throttled"])
def test_peer_reporting_hot_state_is_never_picked(tmp_path, monkeypatch, state):
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    online(tx, "liar", health={"state": state, "temp_c": 30})
    with pytest.raises(RuntimeError, match="hot or throttled"):
        tx.pick_migration_target()
    online(tx, "ok", health={"state": "ok", "temp_c": 60})
    assert tx.pick_migration_target()["node_id"] == "ok"


def test_explicit_target_still_allowed_when_hot(tmp_path, monkeypatch):
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    online(tx, "hot1", health={"temp_c": 95})
    assert tx.pick_migration_target("hot1")["node_id"] == "hot1"  # operator's explicit choice


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), True, "70", None, [1]])
def test_junk_peer_health_and_load_never_crash(tmp_path, monkeypatch, junk):
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    online(tx, "weird", health={"temp_c": junk, "load": junk}, load=junk)
    online(tx, "plain", load=2)
    assert tx.pick_migration_target()["node_id"] in {"weird", "plain"}
    tx.nodes["badrec"] = {"url": "http://x", "last_seen": time.time()}  # no node_id at all
    assert tx.pick_migration_target()["node_id"] != "badrec"


def test_thermal_migrate_with_only_hot_peers_moves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    tx = mkfleet(monkeypatch, tmp_path, "edge", temp=[99.0])
    seed(tx, "a")
    seed(tx, "b")
    online(tx, "hot1", health={"temp_c": 97})
    posted = []

    async def no_post(*a, **kw):  # pragma: no cover - must never be reached
        posted.append(a)
        return {}
    monkeypatch.setattr(tx, "_post", no_post)
    out = run(tx.thermal_migrate())
    assert posted == [] and len(out) == 1 and not out[0]["ok"]  # stops after the first: no target
    assert "hot or throttled" in out[0]["error"]
    assert (agentfs.agents_root(tx.core.settings.workspace) / "a").is_dir()


def test_concurrent_migration_of_same_agent_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    seed(tx)
    online(tx, "cool")
    posts = []

    async def slow_post(base, path, payload, timeout=30):
        posts.append(payload["agent_id"])
        await asyncio.sleep(0.05)
        return {"ok": True}
    monkeypatch.setattr(tx, "_post", slow_post)

    async def both():
        return await asyncio.gather(tx.migrate_agent("builder"), tx.migrate_agent("builder"),
                                    return_exceptions=True)
    a, b = run(both())
    oks = [r for r in (a, b) if isinstance(r, dict)]
    errs = [r for r in (a, b) if isinstance(r, Exception)]
    assert len(oks) == 1 and len(errs) == 1 and isinstance(errs[0], PermissionError)
    assert "already in progress" in str(errs[0]) and posts == ["builder"]
    assert tx._migrating == set()  # released
    assert run(tx.migrate_agent("builder"))["ok"]  # and can run again afterwards


def test_failed_migration_releases_inflight_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    seed(tx)
    with pytest.raises(RuntimeError):
        run(tx.migrate_agent("builder"))  # no peers
    assert tx._migrating == set()


@pytest.mark.parametrize("aid", ["", "../x", "Builder", "a\x00b", "x" * 10_000, "\u00e9t\u00e9"])
def test_odd_agent_ids_refused_and_audit_bounded(tmp_path, monkeypatch, aid):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    tx = mkfleet(monkeypatch, tmp_path, "edge")
    online(tx, "cool")
    with pytest.raises(PermissionError, match="invalid agent id"):
        run(tx.migrate_agent(aid))
    ev = events(tx, "agent.migrate_refused")
    assert ev and all(len(e["agent_id"]) <= 64 for e in ev)


# ---------------------------------------------------------------- agentpack bundles
def _home(tmp_path, aid="builder"):
    ws, data = tmp_path / "ws", tmp_path / "data"
    d = agentfs.agent_dir(ws, aid)
    (d / "notes.txt").write_text("hello")
    os.utime(d / "notes.txt", (1_700_000_000, 1_700_000_000))
    return ws, data


def _tar(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def _members(bundle):
    src = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
    return [(m.name, src.extractfile(m).read()) for m in src.getmembers()]


def _manifest(files, created=1_700_000_000, mem=b"[]", aid="builder"):
    return json.dumps({"format": agentpack.FORMAT, "agent_id": aid, "origin": "edge", "home": "edge",
                       "created": created, "total_bytes": sum(len(d) for _, d in files),
                       "files": [{"path": p, "size": len(d), "sha256": agentpack._sha(d), "mtime": 1}
                                 for p, d in files],
                       "memory": {"namespaces": [], "entries": 0, "sha256": agentpack._sha(mem)}}).encode()


def test_duplicate_manifest_member_refused(tmp_path):
    ws, data = _home(tmp_path)
    b, _ = agentpack.export_pack(ws, data, "builder", "edge")
    ms = _members(b)
    with pytest.raises(PackError, match="duplicate member 'manifest.json'"):
        agentpack.read_pack(_tar(ms + [ms[0]]))
    with pytest.raises(PackError, match="duplicate member 'memory.json'"):
        agentpack.read_pack(_tar(ms + [ms[1]]))


def test_nested_json_manifest_is_packerror(tmp_path):
    deep = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(PackError, match="not JSON"):
        agentpack.read_pack(_tar([("manifest.json", deep), ("memory.json", b"[]")]))
    with pytest.raises(PackError, match="not JSON"):
        agentpack.read_pack(_tar([("manifest.json", _manifest([])), ("memory.json", deep)]))


@pytest.mark.parametrize("created", [True, False, 1.5, "1", None, 2**63, -1])
def test_bad_created_refused(created):
    with pytest.raises(PackError, match="created"):
        agentpack.read_pack(_tar([("manifest.json", _manifest([], created=created)), ("memory.json", b"[]")]))


def test_file_vs_dir_conflict_refused():
    files = [("a", b"x"), ("a/b", b"y")]
    with pytest.raises(PackError, match="path conflict"):
        agentpack.read_pack(_tar([("manifest.json", _manifest(files)), ("memory.json", b"[]")]
                                 + [("home/" + p, d) for p, d in files]))


def test_duplicate_manifest_entry_and_bad_size_refused():
    files = [("a", b"x")]
    man = json.loads(_manifest(files))
    man["files"] = man["files"] * 2
    with pytest.raises(PackError, match="duplicate manifest entry"):
        agentpack.read_pack(_tar([("manifest.json", json.dumps(man).encode()), ("memory.json", b"[]"),
                                  ("home/a", b"x")]))
    man = json.loads(_manifest(files))
    man["files"][0]["size"] = True  # True == 1 == len(b"x") slipped through
    with pytest.raises(PackError, match="bad size"):
        agentpack.read_pack(_tar([("manifest.json", json.dumps(man).encode()), ("memory.json", b"[]"),
                                  ("home/a", b"x")]))


def test_overlong_member_name_is_packerror_not_oserror(tmp_path):
    rel = "n" * 300  # > NAME_MAX on every common filesystem
    files = [(rel, b"x")]
    bundle = _tar([("manifest.json", _manifest(files)), ("memory.json", b"[]"), ("home/" + rel, b"x")])
    with pytest.raises(PackError, match="could not install"):
        agentpack.import_pack(tmp_path / "ws", tmp_path / "data", bundle, "rx")
    root = agentfs.agents_root(tmp_path / "ws")
    assert not (root / "builder").exists() and not list(root.glob(".import-*"))


def test_member_count_cap_is_checked_while_streaming(monkeypatch):
    monkeypatch.setattr(agentpack, "MAX_MEMBERS", 3)
    seen = []
    orig = tarfile.TarFile.next

    def spy(self):
        m = orig(self)
        seen.append(m)
        return m
    monkeypatch.setattr(tarfile.TarFile, "next", spy)
    bundle = _tar([(f"home/f{i}", b"") for i in range(50)])
    with pytest.raises(PackError, match="too many members"):
        agentpack.read_pack(bundle)
    assert len(seen) <= 5  # stopped early instead of indexing all 50 first


def test_corrupt_gzip_is_packerror():
    good = _tar([("manifest.json", _manifest([])), ("memory.json", b"[]")])
    for bad in (good[:20], good[:10] + b"\x00" * 50 + good[60:], b"\x1f\x8b" + b"\xff" * 64, b""):
        with pytest.raises(PackError):
            agentpack.read_pack(bad)


def test_concurrent_imports_of_same_agent_one_wins(tmp_path):
    ws, data = _home(tmp_path / "src")
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    rws, rdata = tmp_path / "rx" / "ws", tmp_path / "rx" / "data"
    results, barrier = [], threading.Barrier(6)

    def imp():
        barrier.wait()
        try:
            results.append(agentpack.import_pack(rws, rdata, bundle, "rx")["files"])
        except PackError as e:
            results.append(str(e))
    ts = [threading.Thread(target=imp) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(1) == 1, results  # exactly one install; the rest: "not newer"
    assert all(r == 1 or "not newer" in r for r in results)
    root = agentfs.agents_root(rws)
    assert (root / "builder" / "notes.txt").read_text() == "hello"
    assert not list(root.glob(".import-*")) and not list(root.glob(".old-*"))


# ---------------------------------------------------------------- agentfs quota boundary + symlink races
def _ctx(tmp_path, aid="a1"):
    d = agentfs.agent_dir(tmp_path / "ws", aid)
    return SimpleNamespace(core=SimpleNamespace(codex=None), run_id="r", name="A-1", agent_id=aid, agent_dir=d), d


def test_quota_exactly_at_limit_ok_one_byte_over_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    ctx, root = _ctx(tmp_path)
    q = quota_bytes()
    quota_write(ctx, root, root / "a.bin", b"x" * (q - 10))
    quota_write(ctx, root, root / "b.bin", b"y" * 10)  # exactly at the quota
    assert dir_usage(root) == q
    with pytest.raises(AgentFSError, match="quota"):
        quota_write(ctx, root, root / "c.bin", b"z")
    with pytest.raises(AgentFSError, match="quota"):
        quota_write(ctx, root, root / "b.bin", b"y", append=True)
    quota_write(ctx, root, root / "b.bin", b"w" * 10)  # same-size rewrite at the limit still fits
    quota_write(ctx, root, root / "b.bin", b"")  # shrinking frees room
    quota_write(ctx, root, root / "c.bin", b"z" * 10)
    assert dir_usage(root) == q


def test_append_does_not_follow_symlink_planted_after_checks(tmp_path, monkeypatch):
    ctx, root = _ctx(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    p = root / "log.txt"
    p.write_text("a")
    import avrone.agentfs_tools as aft
    real = aft.dir_usage

    def racing_usage(d):  # runs between the is_symlink check and the write
        u = real(d)
        p.unlink()
        p.symlink_to(outside)
        return u
    monkeypatch.setattr(aft, "dir_usage", racing_usage)
    with pytest.raises(AgentFSError, match="symlink"):
        quota_write(ctx, root, p, b"PWNED", append=True)
    assert outside.read_text() == "secret"


def test_overwrite_replaces_planted_symlink_not_its_target(tmp_path, monkeypatch):
    ctx, root = _ctx(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    p = root / "f.txt"
    import avrone.agentfs_tools as aft
    real = aft.dir_usage

    def racing_usage(d):
        u = real(d)
        p.symlink_to(outside)
        return u
    monkeypatch.setattr(aft, "dir_usage", racing_usage)
    quota_write(ctx, root, p, b"mine")
    assert outside.read_text() == "secret" and not p.is_symlink() and p.read_bytes() == b"mine"


def test_planted_symlink_at_old_predictable_tmp_name_is_ignored(tmp_path):
    ctx, root = _ctx(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    old_tmp = root / f".f.txt.tmp-{os.getpid()}-{threading.get_ident()}"
    old_tmp.symlink_to(outside)
    quota_write(ctx, root, root / "f.txt", b"mine")
    assert outside.read_text() == "secret" and (root / "f.txt").read_bytes() == b"mine"
    assert len(list(root.glob(".f.txt.tmp-*"))) == 1  # only the planted link; ours was renamed away


# ---------------------------------------------------------------- memstore corruption + huge entries
def _store(tmp_path):
    return memstore.MemoryStore(tmp_path / "memory.jsonl")


@pytest.mark.parametrize("bad_ts", ['"yesterday"', "NaN", "Infinity", "null", "true", "[1]", '{"a":1}'])
def test_corrupt_ts_line_does_not_poison_store(tmp_path, monkeypatch, bad_ts):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "2")
    s = _store(tmp_path)
    s.save("a", "k1", "v1")
    with s.path.open("a") as f:
        f.write('{"ns": "a", "key": "k0", "value": "old", "ts": %s}\n' % bad_ts)
    s.save("a", "k2", "v2")  # space full: eviction sorts by ts (TypeError with a str ts)
    s.save("a", "k3", "v3")
    assert set(s.load("a")) == {"k2", "k3"}
    assert s.trim("a", 1) == 1
    for line in s.path.read_text().splitlines():  # rewrite emitted strict JSON only
        json.loads(line, parse_constant=lambda c: pytest.fail(f"non-standard constant {c}"))


def test_torn_nested_and_nul_lines_skipped(tmp_path):
    s = _store(tmp_path)
    s.save("a", "k", "v")
    with s.path.open("a") as f:
        f.write("[" * 100_000 + "\n")
        f.write('{"ns":"a","key":"x"' + "\x00" * 10 + "\n")
        f.write("\n   \n")
        f.write('{"ns": "a", "key": "torn", "val')  # torn last line, no newline
    assert set(s.load("a")) == {"k"}
    s.save("a", "k2", "v2")
    assert set(s.load("a")) == {"k", "k2"}


def test_huge_value_line_in_file_is_readable_and_save_cap_holds(tmp_path, monkeypatch):
    s = _store(tmp_path)
    with s.path.open("w") as f:
        f.write(json.dumps({"ns": "a", "key": "big", "value": "x" * 5_000_000, "ts": 1}) + "\n")
    assert len(s.load("a")["big"]["value"]) == 5_000_000
    with pytest.raises(memstore.MemoryRefused, match="cap"):
        s.save("a", "k", "y" * (memstore.HARD_MAX_VALUE + 1))
    monkeypatch.setenv("AVRONE_MEMORY_MAX_VALUE", "10")
    assert s.merge("a", [{"key": "m", "value": "\u00e9" * 6, "ts": 5}])["rejected"] == 1  # 12 UTF-8 bytes


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), float("-inf"), "nan", "inf", True, [1], None])
def test_merge_rejects_non_finite_or_junk_ts(tmp_path, ts):
    s = _store(tmp_path)
    s.save("a", "k", "local")
    res = s.merge("a", [{"key": "k", "value": "remote", "ts": ts}])
    assert res["rejected"] == 1 and res["merged"] == 0
    assert s.load("a")["k"]["value"] == "local"


def test_merge_far_future_ts_is_clamped_not_pinned(tmp_path):
    s = _store(tmp_path)
    s.merge("a", [{"key": "k", "value": "skewed", "ts": 9e12}])
    ts = s.load("a")["k"]["ts"]
    assert ts <= time.time() + memstore.MAX_FUTURE_SKEW_S + 1
    # an honest later write (beyond the clamp) can still win
    assert s.merge("a", [{"key": "k", "value": "honest", "ts": ts + 1}])["merged"] == 1
    assert s.load("a")["k"]["value"] == "honest"


@pytest.mark.parametrize("key", ["k\x00ey", "\u202ertl", "x" * 200, "\U0001f600"])
def test_odd_keys_roundtrip(tmp_path, key):
    s = _store(tmp_path)
    s.save("a", key, "v")
    assert _store(tmp_path).load("a")[key]["value"] == "v"


def test_overlong_key_refused(tmp_path):
    with pytest.raises(memstore.MemoryRefused, match="key longer"):
        _store(tmp_path).save("a", "x" * 201, "v")


def test_content_stamp_survives_corrupt_memory(tmp_path):
    ws, data = _home(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    with (data / "memory.jsonl").open("a") as f:
        f.write('{"ns": "builder", "key": "k", "value": "v", "ts": Infinity}\n')
        f.write('{"ns": "builder", "key": "j", "value": "v", "ts": "soon"}\n')
    assert agentpack.content_stamp(ws, data, "builder") == 1_700_000_000
    b, man = agentpack.export_pack(ws, data, "builder", "edge")
    assert man["memory"]["entries"] == 2
