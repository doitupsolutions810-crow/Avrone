"""Agent packs: a portable, verifiable bundle of one agent's home.

An agent's home is its persistent dir (``workspace/agents/<id>/``) plus its durable memory
namespace (``<id>`` in ``data_dir/memory.jsonl``). ``export_pack`` writes both into a
deterministic ``tar.gz``:

    manifest.json   {"format", "agent_id", "origin", "home", "created", "total_bytes",
                     "files": [{"path", "size", "sha256", "mtime"}],
                     "memory": {"namespaces", "entries", "sha256"}}
    memory.json     [{"ns", "key", "value", "ts", "agent"}...]
    home/<relpath>  every regular file of the agent dir

Determinism: entries sorted, tar headers normalised (uid/gid 0, no owner names, mode 0644),
gzip header mtime 0, and ``created`` defaults to the newest content timestamp (file mtimes and
memory ts), so the same home always yields byte-identical bundles.

Export refuses symlinks, devices, fifos and sockets anywhere in the dir, and a home over the
agent-fs quota (AVRONE_AGENT_FS_QUOTA_MB).

Import (``import_pack``) is defensive: bundle size, member count and declared sizes are capped
before anything is extracted; every member must be ``manifest.json``, ``memory.json`` or a
regular ``home/<relpath>`` with no absolute path, no ``..`` and no link/device type; the set of
files must match the manifest exactly and every sha256 must verify. Files are extracted into a
temp dir beside the target and swapped in atomically, and only into ``agents/<manifest agent_id>``.

Overwrite rule: an existing, non-empty home is replaced ONLY when it is the same agent (same
agent id AND same ``home`` node, i.e. where the agent was born) and the pack is strictly newer
than what is there. Memory merges newest-wins into the agent's own local namespace.
Provenance of imported homes is kept in ``data_dir/agentpacks/index.json``.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat as _st
import tarfile
import time
from pathlib import Path
from typing import Any

from . import agentfs, memstore
from .agentfs_tools import dir_usage, quota_bytes

FORMAT = "avrone-agentpack/1"
MANIFEST, MEMORY = "manifest.json", "memory.json"
HOME_PREFIX = "home/"
MAX_MEMBERS = 20000
INDEX_DIR = "agentpacks"


class PackError(ValueError):
    """A pack that cannot be exported or imported safely."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _memory_path(data_dir: Path) -> Path:
    return Path(data_dir) / "memory.jsonl"


def pack_dir(data_dir: Path) -> Path:
    return Path(data_dir) / INDEX_DIR


def _index_path(data_dir: Path) -> Path:
    return pack_dir(data_dir) / "index.json"


def load_index(data_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        d = json.loads(_index_path(data_dir).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(data_dir: Path, idx: dict[str, Any]) -> None:
    p = _index_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".index.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(idx, indent=2, sort_keys=True))
    os.replace(tmp, p)


def record(data_dir: Path, agent_id: str, **kw: Any) -> None:
    idx = load_index(data_dir)
    idx.setdefault(agent_id, {}).update(kw)
    _save_index(data_dir, idx)


def home_node(data_dir: Path, agent_id: str, local_node: str) -> str:
    """Where an agent was born: the recorded home of an imported agent, else this node."""
    return str(load_index(data_dir).get(agent_id, {}).get("home") or local_node)


# ------------------------------------------------------------------ export
def _walk(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    """Every regular file under root as (posix relpath, path, lstat); refuses anything else."""
    out = []
    for cur, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs) + sorted(files):
            p = Path(cur) / name
            st = os.lstat(p)
            rel = p.relative_to(root).as_posix()
            if os.path.islink(p):
                raise PackError(f"refusing symlink in agent home: {rel}")
            if _st.S_ISDIR(st.st_mode):
                continue
            if not _st.S_ISREG(st.st_mode):
                raise PackError(f"refusing special file (device/fifo/socket) in agent home: {rel}")
            out.append((rel, p, st))
    out.sort(key=lambda t: t[0])
    return out


def _memory_records(data_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    mp = _memory_path(data_dir)
    if not mp.exists() or not memstore.is_local(agent_id):
        return []
    space = memstore.MemoryStore(mp).load(agent_id)
    recs = [{"ns": agent_id, "key": str(r["key"]), "value": str(r.get("value", "")),
             "ts": float(r.get("ts", 0)), "agent": str(r.get("agent", ""))[:100]} for r in space.values()]
    recs.sort(key=lambda r: (r["key"], r["ts"]))
    return recs


def content_stamp(workspace: Path, data_dir: Path, agent_id: str) -> int:
    """Newest content timestamp (int seconds) of an agent's home: file mtimes + memory ts."""
    d = agentfs.agents_root(workspace) / agentfs.sanitize_agent_id(agent_id)
    stamp = 0
    if d.is_dir() and not d.is_symlink():
        for cur, _dirs, files in os.walk(d, followlinks=False):
            for f in files:
                try:
                    stamp = max(stamp, int(os.lstat(os.path.join(cur, f)).st_mtime))
                except OSError:
                    continue
    for r in _memory_records(data_dir, agentfs.sanitize_agent_id(agent_id)):
        stamp = max(stamp, int(r["ts"]))
    return stamp


def _tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode = size, mtime, 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.type = tarfile.REGTYPE
    return ti


def export_pack(workspace: Path, data_dir: Path, agent_id: str, origin: str, home: str | None = None,
                created: int | None = None) -> tuple[bytes, dict[str, Any]]:
    """Bundle an agent's home. Returns (tar.gz bytes, manifest)."""
    sid = agentfs.sanitize_agent_id(agent_id)
    if sid != str(agent_id):
        raise PackError(f"invalid agent id {agent_id!r}")
    d = agentfs.agents_root(workspace) / sid
    if d.is_symlink():
        raise PackError("agent dir must not be a symlink")
    files = _walk(d) if d.is_dir() else []
    total = sum(st.st_size for _, _, st in files)
    quota = quota_bytes()
    if total > quota:
        raise PackError(f"agent home is {total} bytes; over the agent-fs quota of {quota} bytes")
    blobs: list[tuple[str, bytes, int]] = []
    entries = []
    for rel, p, st in files:
        data = p.read_bytes()
        if len(data) != st.st_size:
            raise PackError(f"{rel} changed while packing")
        mt = int(st.st_mtime)
        blobs.append((HOME_PREFIX + rel, data, mt))
        entries.append({"path": rel, "size": len(data), "sha256": _sha(data), "mtime": mt})
    mem = _memory_records(data_dir, sid)
    mem_bytes = json.dumps(mem, sort_keys=True, separators=(",", ":")).encode()
    if created is None:
        created = max([e["mtime"] for e in entries] + [int(r["ts"]) for r in mem] + [0])
    manifest = {"format": FORMAT, "agent_id": sid, "origin": str(origin), "home": str(home or origin),
                "created": int(created), "total_bytes": total, "files": entries,
                "memory": {"namespaces": [sid] if mem else [], "entries": len(mem), "sha256": _sha(mem_bytes)}}
    man_bytes = json.dumps(manifest, sort_keys=True, indent=1).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data, mt in [(MANIFEST, man_bytes, int(created)), (MEMORY, mem_bytes, int(created))] + blobs:
            tf.addfile(_tarinfo(name, len(data), mt), io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=6) as gz:
        gz.write(raw.getvalue())
    return out.getvalue(), manifest


# ------------------------------------------------------------------ import
def _check_rel(rel: str) -> str:
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise PackError(f"bad path in pack: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or "\\" in rel:
        raise PackError(f"path traversal in pack: {rel!r}")
    return rel


def read_pack(bundle: bytes) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, bytes]]:
    """Parse and fully verify a bundle in memory. Returns (manifest, memory records, {rel: bytes})."""
    quota = quota_bytes()
    if len(bundle) > quota + 1024 * 1024:
        raise PackError(f"bundle is {len(bundle)} bytes; over the quota")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
        members = tf.getmembers()
    except (tarfile.TarError, OSError, EOFError) as e:
        raise PackError(f"not a valid agent pack: {e}") from None
    if len(members) > MAX_MEMBERS:
        raise PackError("too many members in pack")
    declared = 0
    blobs: dict[str, bytes] = {}
    man = mem = None
    for m in members:
        if not m.isreg():
            raise PackError(f"refusing non-regular member (link/device/dir) {m.name!r}")
        declared += m.size
        if declared > quota + 1024 * 1024:
            raise PackError("pack contents exceed the quota")
        f = tf.extractfile(m)
        data = f.read() if f else b""
        if m.name == MANIFEST:
            man = data
        elif m.name == MEMORY:
            mem = data
        elif m.name.startswith(HOME_PREFIX):
            rel = _check_rel(m.name[len(HOME_PREFIX):])
            if rel in blobs:
                raise PackError(f"duplicate member {rel!r}")
            blobs[rel] = data
        else:
            raise PackError(f"unexpected member {m.name!r}")
    if man is None or mem is None:
        raise PackError("pack has no manifest/memory")
    try:
        manifest = json.loads(man)
        memory = json.loads(mem)
    except ValueError:
        raise PackError("manifest/memory is not JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise PackError("unknown pack format")
    aid = manifest.get("agent_id")
    if not isinstance(aid, str) or agentfs.sanitize_agent_id(aid) != aid:
        raise PackError("bad agent_id in manifest")
    for k in ("origin", "home"):
        if not isinstance(manifest.get(k), str) or not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", manifest[k]):
            raise PackError(f"bad {k} in manifest")
    if not isinstance(manifest.get("created"), int):
        raise PackError("bad created in manifest")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PackError("bad files list in manifest")
    listed = {}
    for e in files:
        if not isinstance(e, dict):
            raise PackError("bad file entry")
        rel = _check_rel(str(e.get("path", "")))
        listed[rel] = e
    if set(listed) != set(blobs):
        raise PackError("pack files do not match the manifest")
    total = 0
    for rel, e in listed.items():
        data = blobs[rel]
        if len(data) != e.get("size") or _sha(data) != e.get("sha256"):
            raise PackError(f"hash/size mismatch for {rel!r} (tampered pack)")
        total += len(data)
    if total > quota:
        raise PackError(f"pack holds {total} bytes; over the agent-fs quota of {quota} bytes")
    if _sha(mem) != (manifest.get("memory") or {}).get("sha256"):
        raise PackError("memory hash mismatch (tampered pack)")
    if not isinstance(memory, list):
        raise PackError("memory must be a list")
    return manifest, memory, blobs


def import_pack(workspace: Path, data_dir: Path, bundle: bytes, local_node: str,
                expect_agent: str | None = None, expect_origin: str | None = None) -> dict[str, Any]:
    """Verify and install a bundle into agents/<agent_id>. Raises PackError on any refusal."""
    manifest, memory, blobs = read_pack(bundle)
    aid = manifest["agent_id"]
    if expect_agent is not None and aid != expect_agent:
        raise PackError(f"pack is for agent {aid!r}, not {expect_agent!r}")
    if expect_origin is not None and manifest["origin"] != expect_origin:
        raise PackError(f"pack origin {manifest['origin']!r} does not match sender {expect_origin!r}")
    root = agentfs.agents_root(workspace)
    if root.is_symlink():
        raise PackError("agents root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    target = root / aid
    if target.is_symlink():
        raise PackError("agent dir must not be a symlink")
    existing = target.is_dir() and any(target.iterdir())
    have_mem = bool(_memory_records(data_dir, aid))
    if existing or have_mem:
        local_home = home_node(data_dir, aid, local_node)
        if manifest["home"] != local_home:
            raise PackError(f"agent dir {aid!r} belongs to another agent (home {local_home}, "
                            f"pack home {manifest['home']}); refusing to overwrite")
        stamp = content_stamp(workspace, data_dir, aid)
        if manifest["created"] <= stamp:
            raise PackError(f"pack (created {manifest['created']}) is not newer than the local home "
                            f"({stamp}); refusing to overwrite")
    tmp = root / f".import-{aid}-{os.getpid()}-{int(time.time() * 1000)}"
    old = None
    try:
        tmp.mkdir(mode=0o700)
        by_path = {e["path"]: e for e in manifest["files"]}
        for rel, data in sorted(blobs.items()):
            dest = (tmp / rel).resolve()
            if not dest.is_relative_to(tmp.resolve()):
                raise PackError(f"path escapes the agent dir: {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            mt = int(by_path[rel].get("mtime") or manifest["created"])
            os.utime(dest, (mt, mt))
        if target.exists():
            old = root / f".old-{aid}-{os.getpid()}-{int(time.time() * 1000)}"
            os.replace(target, old)
        os.replace(tmp, target)
        os.chmod(target, 0o700)
    except BaseException:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if old is not None and old.exists() and not target.exists():
            os.replace(old, target)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    stats = {"merged": 0, "skipped": 0, "rejected": 0}
    good = [r for r in memory if isinstance(r, dict)]
    stats["rejected"] += len(memory) - len(good)
    if good:
        stats = {k: stats[k] + v for k, v in
                 memstore.MemoryStore(_memory_path(data_dir)).merge(aid, good, source=manifest["origin"]).items()}
    if manifest["home"] != local_node:
        record(data_dir, aid, home=manifest["home"], origin=manifest["origin"], created=manifest["created"],
               imported=int(time.time()))
    return {"agent_id": aid, "origin": manifest["origin"], "home": manifest["home"],
            "created": manifest["created"], "files": len(blobs), "bytes": manifest["total_bytes"],
            "memory": stats}
