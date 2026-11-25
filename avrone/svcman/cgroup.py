"""Optional cgroup v2 limits. Missing controllers are skipped, never required."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .spec import LimitSpec

_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def current_cgroup() -> Path | None:
    proc = Path("/proc/self/cgroup")
    mount = Path("/sys/fs/cgroup")
    if not proc.is_file() or not (mount / "cgroup.controllers").is_file():
        return None
    try:
        text = proc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            relative = parts[2].lstrip("/")
            path = mount / relative if relative else mount
            if path.is_dir():
                return path
    return None


def apply(name: str, pid: int, limits: LimitSpec, root: Path | None = None) -> tuple[bool, str]:
    """Put `pid` in a child cgroup and set memory.max / cpu.max / pids.max.

    Returns (applied, detail). When the kernel has not delegated controllers,
    returns (False, reason) unless `limits.cgroup_required`.
    """
    if not limits.has_cgroup():
        return True, "no cgroup limits requested"
    base = Path(root) if root is not None else current_cgroup()
    if base is None:
        return _fail(limits, "cgroup v2 is not mounted")
    safe = _NAME.sub("-", name).strip("-") or "service"
    try:
        group = _prepare(base, safe, limits)
        (group / "cgroup.procs").write_text(str(int(pid)), encoding="utf-8")
    except OSError as exc:
        return _fail(limits, f"cgroup v2 not applied: {exc}")
    return True, str(group)


def _prepare(base: Path, name: str, limits: LimitSpec) -> Path:
    parent = base / "avrone-svc"
    parent.mkdir(exist_ok=True)
    _delegate(base, parent)
    group = parent / name
    group.mkdir(exist_ok=True)
    if limits.memory_max:
        _write(group / "memory.max", limits.memory_max)
    if limits.cpu_max:
        _write(group / "cpu.max", limits.cpu_max)
    if limits.pids_max:
        _write(group / "pids.max", limits.pids_max)
    return group


def _delegate(base: Path, parent: Path) -> None:
    """Enable controllers on the parent when this cgroup has no member processes.

    A cgroup that already contains processes cannot delegate. Callers then see
    a missing memory.max file and the limit write fails closed into the skip path.
    """
    controllers = _read(base / "cgroup.controllers")
    subtree = parent / "cgroup.subtree_control"
    # Delegate from `base` into its children when the kernel allows it.
    wanted = [token for token in ("memory", "cpu", "pids", "io") if token in controllers.split()]
    if not wanted:
        return
    current = _read(base / "cgroup.subtree_control").split()
    missing = [token for token in wanted if f"+{token}" not in current and token not in current]
    if missing and not _read(base / "cgroup.procs").strip():
        _write(base / "cgroup.subtree_control", " ".join(f"+{token}" for token in missing))
    if subtree.is_file() and not _read(parent / "cgroup.procs").strip():
        child_have = _read(parent / "cgroup.controllers").split()
        child_missing = [token for token in wanted if token in child_have]
        if child_missing and not _read(subtree).strip():
            try:
                _write(subtree, " ".join(f"+{token}" for token in child_missing))
            except OSError:
                return


def _write(path: Path, text: str) -> None:
    path.write_text(str(text).strip() + "\n", encoding="utf-8")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _fail(limits: LimitSpec, detail: str) -> tuple[bool, str]:
    if limits.cgroup_required:
        raise OSError(detail)
    return False, detail
