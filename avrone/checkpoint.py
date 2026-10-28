"""Checkpoint / resume for long squad and deep runs.

A long run on an edge node can be killed halfway (power, thermal shutdown, a
dropped SSH session). With a checkpoint file, Trinity records progress as it
goes: the plan, each worker's (or reviewer's) finished result, each completed
deep round, and finally the whole result. Re-running the same task with the
same checkpoint file skips everything already done and continues from there.

File format (JSON, written atomically via a temp file + os.replace):

    {"version": 1, "updated": <unix ts>, "steps": {"<key>": <json value>, ...}}

Keys are namespaced by scope, e.g. "meta", "subtasks", "worker.0", "result" for a
squad; "round1.reviewer.2", "rounds", "first_answer", "wave1.worker.0" for deep
(the first-wave squad of a deep run lives under the "wave1." scope).

Rules: a scope is reused only when its "meta" (task, mode and size) matches the
new call; otherwise that scope is wiped and the run starts fresh. Failed worker
results ("[X failed: ...]") are never stored, so a resume retries them. A
corrupt or unreadable file is treated as empty, and a write that fails never
breaks the run (the checkpoint is best effort).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

VERSION = 1


class _Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": VERSION, "steps": {}}
        self.write_errors = 0
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get("steps"), dict) and raw.get("version") == VERSION:
                self.data = raw
        except (OSError, ValueError):
            pass

    def save(self) -> bool:
        self.data["updated"] = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, default=str)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, TypeError, ValueError):
            self.write_errors += 1
            return False


class Checkpoint:
    """A (scoped) view on one checkpoint file."""

    def __init__(self, path: str | Path | _Store, prefix: str = ""):
        self._store = path if isinstance(path, _Store) else _Store(Path(path))
        self.prefix = prefix

    @classmethod
    def coerce(cls, obj: "Checkpoint | str | Path | None") -> "Checkpoint | None":
        """None -> None; a path -> a Checkpoint on that file; a Checkpoint -> itself."""
        if obj is None or obj == "" or obj is False:
            return None
        if isinstance(obj, Checkpoint):
            return obj
        return cls(Path(obj))

    @property
    def path(self) -> Path:
        return self._store.path

    def scope(self, name: str) -> "Checkpoint":
        """A sub-view whose keys live under '<prefix><name>' in the same file."""
        return Checkpoint(self._store, self.prefix + name)

    # ------------------------------------------------------------ key/value
    def get(self, key: str, default: Any = None) -> Any:
        return self._store.data["steps"].get(self.prefix + key, default)

    def put(self, key: str, value: Any) -> None:
        self._store.data["steps"][self.prefix + key] = value
        self._store.save()

    def keys(self) -> list[str]:
        n = len(self.prefix)
        return [k[n:] for k in self._store.data["steps"] if k.startswith(self.prefix)]

    def clear(self) -> None:
        """Drop every key in this scope (other scopes in the file are kept)."""
        steps = self._store.data["steps"]
        for k in [k for k in steps if k.startswith(self.prefix)]:
            del steps[k]
        self._store.save()

    # ------------------------------------------------------------ run binding
    def resume_point(self, task: str, mode: str, **extra: Any) -> dict[str, Any] | None:
        """The stored meta if this scope holds a run of the same task/mode/extra, else None.

        A scope holding a different run is cleared so stale results can never leak in."""
        meta = self.get("meta")
        want = {"task": task, "mode": mode, **extra}
        if isinstance(meta, dict) and all(meta.get(k) == v for k, v in want.items()):
            return meta
        if self.keys():
            self.clear()
        return None

    def begin(self, task: str, mode: str, run_id: str, **extra: Any) -> None:
        meta = self.get("meta")
        if not (isinstance(meta, dict) and meta.get("run_id") == run_id and meta.get("task") == task):
            self.put("meta", {"task": task, "mode": mode, "run_id": run_id, "started": time.time(), **extra})

    def summary(self) -> dict[str, Any]:
        keys = self.keys()
        return {"path": str(self.path), "steps": len(keys),
                "done": self.get("result") is not None, "write_errors": self._store.write_errors}


# ---------------------------------------------------------------- server-managed checkpoints
# Over the API, MCP, CLI (--checkpoint-id) and fleet, a client names a checkpoint by id only.
# The file always lives at <data_dir>/checkpoints/<key>.json; a client never supplies a path.
# Fleet jobs are keyed "fleet-<sender>-<id>" and stamped with their owner, so one sender can
# neither collide with nor resume (read) another sender's checkpoint.
CKPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
FLEET_PREFIX = "fleet-"
CKPT_MODES = ("squad", "deep")
_active: set[str] = set()  # resolved paths of checkpoints a run in this process is using


class CheckpointError(ValueError):
    """A bad checkpoint id (-> HTTP 400)."""


class CheckpointConflict(ValueError):
    """The id holds a different task/mode/owner, or is in use by a running job (-> HTTP 409)."""


def sanitize_id(raw: Any) -> str:
    """A client checkpoint id: 1-64 chars of [A-Za-z0-9_-], else CheckpointError."""
    if not isinstance(raw, str) or not CKPT_ID_RE.match(raw):
        raise CheckpointError("checkpoint_id must be 1-64 characters of A-Z a-z 0-9 _ -")
    return raw


def sanitize_client_id(raw: Any) -> str:
    """sanitize_id plus: the fleet- prefix is reserved for checkpoints of remote jobs."""
    cid = sanitize_id(raw)
    if cid.startswith(FLEET_PREFIX):
        raise CheckpointError(f"checkpoint ids starting with {FLEET_PREFIX!r} are reserved for fleet jobs")
    return cid


def fleet_key(sender: str, ckpt_id: Any) -> str:
    cid = sanitize_id(ckpt_id)
    if not isinstance(sender, str) or not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", sender):
        raise CheckpointError("fleet checkpoint needs a valid sender node id")
    return f"{FLEET_PREFIX}{sender}-{cid}"


def managed_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "checkpoints"


def managed_path(data_dir: str | Path, key: str) -> Path:
    """The file for a (validated) key; never escapes <data_dir>/checkpoints."""
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise CheckpointError("bad checkpoint id")
    return managed_dir(data_dir) / f"{key}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("steps"), dict) else None


def check_compatible(path: str | Path, task: str, mode: str, owner: str | None = None) -> dict[str, Any] | None:
    """Raise CheckpointConflict unless `path` is free, unreadable, or holds this same task/mode/owner.

    Returns the stored top-level meta (a resume) or None (a fresh start)."""
    path = Path(path)
    key = path.stem
    if is_busy(path):
        raise CheckpointConflict(f"checkpoint {key!r} is in use by a running job; wait for it or use another id")
    data = _read(path)
    if data is None:
        return None
    if data.get("owner") != owner:
        raise CheckpointConflict(f"checkpoint {key!r} belongs to another owner; use a different checkpoint_id")
    meta = data["steps"].get("meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("mode") != mode or meta.get("task") != task:
        raise CheckpointConflict(
            f"checkpoint {key!r} holds a different {meta.get('mode')} task "
            f"({_preview(meta.get('task'))!r}); re-post the same task to resume, "
            f"use a new checkpoint_id, or DELETE /api/checkpoints/{key}")
    return meta


def stamp_owner(path: str | Path, owner: str) -> None:
    """Record who owns a fleet checkpoint before its run starts writing it."""
    path = Path(path)
    data = _read(path) or {"version": VERSION, "steps": {}}
    if data.get("owner") == owner:
        return
    store = _Store(path)
    store.data = {**data, "owner": owner}
    store.save()


def is_busy(path: str | Path) -> bool:
    return str(Path(path).resolve()) in _active


def claim(path: str | Path) -> Any:
    """Mark the checkpoint in use; returns a release() callable. CheckpointConflict when taken."""
    k = str(Path(path).resolve())
    if k in _active:
        raise CheckpointConflict(f"checkpoint {Path(path).stem!r} is in use by a running job")
    _active.add(k)
    return lambda: _active.discard(k)


@contextmanager
def claimed(path: str | Path):
    release = claim(path)
    try:
        yield
    finally:
        release()


def _preview(task: Any, n: int = 120) -> str:
    t = " ".join(str(task or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def describe(path: str | Path, full: bool = False) -> dict[str, Any] | None:
    """Summary of one managed checkpoint file (None if missing/unreadable)."""
    path = Path(path)
    data = _read(path)
    if data is None:
        return None
    steps = data["steps"]
    meta = steps.get("meta") if isinstance(steps.get("meta"), dict) else {}
    result = steps.get("result")
    rounds = steps.get("rounds") if isinstance(steps.get("rounds"), dict) else {}
    out: dict[str, Any] = {
        "id": path.stem, "mode": meta.get("mode"), "task": _preview(meta.get("task")),
        "done_steps": sum(1 for k in steps if not (k == "meta" or k.endswith(".meta"))),
        "done": isinstance(result, dict), "updated": data.get("updated"), "busy": is_busy(path),
    }
    if full:
        out.update(run_id=meta.get("run_id"), started=meta.get("started"), owner=data.get("owner"),
                   squad_size=meta.get("n"), keys=sorted(steps),
                   rounds_done=len(rounds.get("history") or []))
        if isinstance(result, dict):
            out["answer_preview"] = _preview(result.get("answer") or result.get("report"), 400)
    return out


def list_managed(data_dir: str | Path) -> list[dict[str, Any]]:
    d = managed_dir(data_dir)
    rows = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        if _KEY_RE.match(p.stem) and (row := describe(p)) is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return rows


def delete_managed(data_dir: str | Path, key: str) -> bool:
    """True if deleted, False if missing. CheckpointConflict while a run uses it."""
    p = managed_path(data_dir, key)
    if is_busy(p):
        raise CheckpointConflict(f"checkpoint {key!r} is in use by a running job")
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
