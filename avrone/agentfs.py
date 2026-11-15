"""Per-agent persistent filesystem workspaces.

Every agent gets its OWN directory under the shared workspace that survives across
runs, so it can keep files between tasks:

    <settings.workspace>/agents/<agent_id>/

The agent id is derived from a stable name (by default the agent's role, or an
explicit ``agent_id``) and sanitized to ``[a-z0-9_-]{1,48}``, so the directory is
always exactly one level below ``workspace/agents`` and can never escape it
(no ``..``, no separators, no absolute paths, no symlinked dirs).

Inside the Docker runner the same host directory is reachable two ways: through the
shared ``/workspace`` bind mount (``/workspace/agents/<id>``, used by the long-lived
environment container) and, in isolated mode, as a dedicated per-agent volume
``-v <host agent dir>:/agent``.

Options (env, read at call time so config.py needs no change):
    AVRONE_AGENT_FS                 1/0  give agents their own dir (default 1)
    AVRONE_AGENT_DOCKER_ISOLATE     1/0  docker: run each agent command in a throwaway
                                         container that mounts ONLY the agent dir at
                                         /agent (default 0: exec in the shared container
                                         with cwd /workspace/agents/<id>)
"""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path

AGENTS_SUBDIR = "agents"
CONTAINER_AGENT_MOUNT = "/agent"
CONTAINER_WORKSPACE = "/workspace"
MAX_ID_LEN = 48

_BAD = re.compile(r"[^a-z0-9_-]+")


class AgentFSError(ValueError):
    """An agent path that is invalid or would escape its confinement."""


def _truthy(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def agent_fs_enabled() -> bool:
    return _truthy(os.environ.get("AVRONE_AGENT_FS"), True)


def docker_isolate() -> bool:
    return _truthy(os.environ.get("AVRONE_AGENT_DOCKER_ISOLATE"), False)


def sanitize_agent_id(raw: object) -> str:
    """Map any agent name/role to a safe, stable directory name.

    Lowercased; runs of anything outside [a-z0-9_-] collapse to '-'; leading/trailing
    '-', '_' and '.' are stripped; capped at MAX_ID_LEN. Empty results become 'agent'.
    The output never contains '/', '\\', '.' or '..'."""
    s = _BAD.sub("-", str(raw if raw is not None else "").strip().lower())
    s = s.strip("-_")[:MAX_ID_LEN].strip("-_")
    return s or "agent"


def agents_root(workspace: Path) -> Path:
    return Path(workspace).resolve() / AGENTS_SUBDIR


def _secure_private_directory(path: Path) -> None:
    """Verify the final path component is a directory and force owner-only permissions."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AgentFSError(f"directory {path} is not a safe, accessible directory") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise AgentFSError(f"path {path} is not a directory")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o700)
            else:
                os.chmod(path, 0o700)
        except OSError as exc:
            raise AgentFSError(f"could not enforce private permissions on {path}") from exc
    finally:
        os.close(fd)


def agent_dir(workspace: Path, agent_id: object, create: bool = True) -> Path:
    """The confined, persistent directory for `agent_id` (created on demand).

    Raises AgentFSError if the result would not sit directly under workspace/agents
    (e.g. a symlink planted there pointing elsewhere)."""
    ws = Path(workspace).resolve()
    root = ws / AGENTS_SUBDIR
    sid = sanitize_agent_id(agent_id)
    if root.is_symlink():
        raise AgentFSError("agents root must not be a symlink")
    p = root / sid
    if p.is_symlink():
        raise AgentFSError(f"agent dir {sid!r} must not be a symlink")
    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentFSError("agents root could not be created safely") from exc
    if root.exists():
        _secure_private_directory(root)
    if create:
        try:
            p.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise AgentFSError(f"agent dir {sid!r} could not be created safely") from exc
    if p.is_symlink():
        raise AgentFSError(f"agent dir {sid!r} must not be a symlink")
    if p.exists():
        _secure_private_directory(p)
    rp = p.resolve()
    if rp.parent != root.resolve() or not rp.is_relative_to(ws):
        raise AgentFSError("agent dir escapes the workspace")
    return rp


def is_agent_dir(workspace: Path, path: Path | None) -> bool:
    """True only for an existing canonical, non-symlink workspace/agents/<id> directory."""
    if path is None:
        return False
    root = agents_root(workspace)
    raw = Path(path)
    try:
        # Do not resolve first: resolving a symlink to a sibling home would erase evidence
        # that the caller supplied an alias instead of this agent's own directory.
        if root.is_symlink() or raw.parent != root or raw.is_symlink():
            return False
        rp = raw.resolve(strict=True)
        return (
            rp == raw
            and rp.parent == root
            and sanitize_agent_id(rp.name) == rp.name
            and rp.is_dir()
        )
    except OSError:
        return False


def _canonical_directory_root(path: Path) -> Path:
    """Return a trusted canonical directory path; reject a root symlink substitution."""
    raw = Path(path)
    resolved = raw.resolve(strict=True)
    if raw != resolved or raw.is_symlink() or not resolved.is_dir():
        raise AgentFSError("confined filesystem root is not a canonical, non-symlink directory")
    return resolved


def _open_directory_chain(path: Path) -> int:
    """Open a directory by walking every component with O_NOFOLLOW.

    The returned descriptor pins the directory even if an attacker renames its pathname
    after validation. Descriptor-relative traversal is required for this guarantee.
    """
    if os.name != "posix" or os.open not in getattr(os, "supports_dir_fd", set()):
        raise AgentFSError("safe descriptor-relative filesystem access is unavailable on this platform")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise AgentFSError("platform lacks required no-follow directory support")
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise AgentFSError(f"{resolved} is not a directory")
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(resolved.anchor, os.O_RDONLY | directory | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in resolved.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_confined_parent(root: Path, target: Path, *, create_parents: bool = False) -> tuple[int, str]:
    """Pin the resolved target's parent beneath root and return (parent_fd, leaf_name)."""
    base = _canonical_directory_root(root)
    resolved = Path(target).resolve(strict=False)
    if not resolved.is_relative_to(base) or resolved == base:
        raise AgentFSError("path escapes its confined filesystem root or names the root itself")
    relative = resolved.relative_to(base)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise AgentFSError("invalid confined path")
    fd = _open_directory_chain(base)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, relative.parts[-1]
    except BaseException:
        os.close(fd)
        raise


def open_confined_file(root: Path, target: Path) -> int:
    """Open one regular file beneath root without following any path-component symlink."""
    parent_fd, leaf = _open_confined_parent(root, target)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(leaf, flags, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise AgentFSError("confined path is not a regular file")
        return fd
    finally:
        os.close(parent_fd)


def read_confined_bytes(root: Path, target: Path, max_bytes: int | None = None) -> bytes:
    """Read a confined regular file through a pinned descriptor."""
    fd = open_confined_file(root, target)
    with os.fdopen(fd, "rb") as handle:
        return handle.read() if max_bytes is None else handle.read(max_bytes + 1)


def write_confined_file(root: Path, target: Path, data: bytes, *, append: bool = False,
                        max_bytes: int | None = None) -> int:
    """Write beneath root with a pinned parent and descriptor-relative atomic replacement."""
    parent_fd, leaf = _open_confined_parent(root, target, create_parents=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        os.close(parent_fd)
        raise AgentFSError("platform lacks no-follow file support")
    try:
        if append:
            try:
                fd = os.open(leaf, os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow |
                             getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise AgentFSError("refusing a symlink at the file leaf") from None
                raise
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise AgentFSError("append target must be a regular, unlinked file")
                size = metadata.st_size + len(data)
                if max_bytes is not None and size > max_bytes:
                    raise AgentFSError(f"file would exceed the {max_bytes}-byte limit")
                with os.fdopen(fd, "ab") as handle:
                    fd = -1
                    handle.write(data)
                return size
            finally:
                if fd >= 0:
                    os.close(fd)

        size = len(data)
        if max_bytes is not None and size > max_bytes:
            raise AgentFSError(f"file would exceed the {max_bytes}-byte limit")
        temp = f".avrone-write-{os.getpid()}-{os.urandom(16).hex()}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow |
                     getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
            os.replace(temp, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        return size
    finally:
        os.close(parent_fd)


def _open_confined_directory(root: Path, target: Path) -> int:
    """Open a directory beneath root through no-follow descriptor-relative traversal."""
    base = _canonical_directory_root(root)
    resolved = Path(target).resolve(strict=True)
    if not resolved.is_relative_to(base):
        raise AgentFSError("directory escapes its confined filesystem root")
    relative = resolved.relative_to(base)
    fd = _open_directory_chain(base)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        for component in relative.parts:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_directory_contents(directory_fd: int) -> int:
    """Recursively unlink entries relative to pinned directory descriptors."""
    freed = 0
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                freed += _remove_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
            freed += metadata.st_size
    return freed


def delete_confined_entry(root: Path, parent: Path, leaf: str, *, recursive: bool = False) -> int:
    """Delete one entry beneath root without following a swapped parent or leaf symlink."""
    if type(recursive) is not bool:
        raise AgentFSError("recursive must be a boolean")
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\\" in leaf or "\\x00" in leaf:
        raise AgentFSError("invalid delete entry name")
    parent_fd = _open_confined_directory(root, parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(leaf, flags, dir_fd=parent_fd)
            try:
                if not recursive and os.listdir(child_fd):
                    raise AgentFSError("directory is non-empty")
                freed = _remove_directory_contents(child_fd) if recursive else 0
            finally:
                os.close(child_fd)
            os.rmdir(leaf, dir_fd=parent_fd)
            return freed
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise AgentFSError("not a file or directory")
        os.unlink(leaf, dir_fd=parent_fd)
        return metadata.st_size
    finally:
        os.close(parent_fd)


def confine(base: Path, rel: object) -> Path:
    """Resolve a RELATIVE path inside `base`; reject absolute paths, '..' and escapes."""
    raw = str(rel if rel is not None else "").strip()
    if not raw or raw in {".", "./"}:
        return Path(base).resolve()
    if "\x00" in raw:
        raise AgentFSError("path contains a NUL byte")
    if raw.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", raw) or Path(raw).is_absolute():
        raise AgentFSError("absolute paths are not allowed; use a path relative to your agent dir")
    parts = re.split(r"[\\/]+", raw)
    if any(part == ".." for part in parts):
        raise AgentFSError("'..' is not allowed in agent paths")
    b = Path(base).resolve()
    p = (b / raw).resolve()
    if not p.is_relative_to(b):
        raise AgentFSError("path escapes your agent dir")
    return p


def container_agent_path(workspace: Path, host_dir: Path) -> str:
    """Where a host agent dir appears inside the shared container (/workspace/agents/<id>)."""
    rel = Path(host_dir).resolve().relative_to(Path(workspace).resolve())
    return f"{CONTAINER_WORKSPACE}/{rel.as_posix()}"


def agent_volume_args(host_dir: Path) -> list[str]:
    """docker args that mount one agent dir as its private volume at /agent."""
    return ["-v", f"{Path(host_dir).resolve()}:{CONTAINER_AGENT_MOUNT}"]
