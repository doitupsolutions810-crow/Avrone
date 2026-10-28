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
import tempfile
import time
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
