"""Spawn service processes in their own session, with rlimits and reaping helpers.

The child applies rlimits and an optional setuid through a tiny Python bootstrap
and then execs the real command. That keeps `preexec_fn` out of a process that
already has threads (the control API and the log readers).
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .spec import LimitSpec

# rlimits are applied before setuid so a privileged supervisor can lower them
# and then drop to the service user. The bootstrap then replaces itself.
BOOTSTRAP = r"""
import json, os, resource, sys
boot = json.loads(os.environ.pop("AVRONE_SVC_BOOT", "{}") or "{}")
for name, soft, hard in boot.get("rlimits") or []:
    resource.setrlimit(getattr(resource, name), (int(soft), int(hard)))
user = boot.get("user") or ""
if user:
    import pwd
    pw = pwd.getpwnam(user)
    if os.geteuid() != pw.pw_uid:
        try:
            os.initgroups(user, pw.pw_gid)
        except OSError:
            pass
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
argv = sys.argv[1:]
if not argv:
    sys.stderr.write("avrone-svc bootstrap: empty command\n")
    sys.exit(127)
os.execvpe(argv[0], argv, os.environ)
"""


@dataclass
class Child:
    pid: int
    popen: subprocess.Popen
    argv: list[str]


def binary_exists(command: str) -> bool:
    if not command:
        return False
    if os.path.isabs(command) or "/" in command:
        return os.path.isfile(command) and os.access(command, os.X_OK)
    return shutil.which(command) is not None


def can_switch_user(user: str) -> tuple[bool, str]:
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return False, f"unknown user {user}"
    if os.geteuid() == entry.pw_uid:
        return True, ""
    if os.geteuid() != 0:
        return False, f"not root, cannot switch to user {user}"
    return True, ""


def spawn(command: list[str], cwd: str | None, env: dict[str, str], user: str | None, limits: LimitSpec | None) -> Child:
    argv = list(command)
    child_env = dict(env)
    child_env.pop("AVRONE_SVC_BOOT", None)
    use_wrapper = bool(user) or (limits is not None and limits.has_rlimits())
    if use_wrapper:
        payload = {
            "rlimits": limits.rlimit_triples() if limits is not None else [],
            "user": user or "",
        }
        child_env["AVRONE_SVC_BOOT"] = json.dumps(payload)
        argv = [sys.executable, "-c", BOOTSTRAP, *command]
    popen = subprocess.Popen(
        argv,
        cwd=cwd or None,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    return Child(pid=popen.pid, popen=popen, argv=argv)


def alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pid: int, sig: int) -> None:
    """Signal the service's process group. Fall back to the pid if it is not a leader."""
    if not pid or pid <= 0:
        return
    try:
        os.killpg(pid, sig)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return


def decode_status(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return -1


def read_environ(pid: int) -> dict[str, str] | None:
    path = f"/proc/{pid}/environ"
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    env: dict[str, str] = {}
    for item in data.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def matches_identity(pid: int | None, name: str, nonce: str | None) -> bool:
    """True when `pid` is still the process we spawned (same service nonce)."""
    if not pid or not nonce or not alive(pid):
        return False
    env = read_environ(pid)
    if env is None:
        return False
    return env.get("AVRONE_SVC_ID") == f"{name}:{nonce}"


def enable_subreaper() -> bool:
    """Become a child subreaper so orphaned grandchildren are reaped here.

    Pid 1 already receives orphans. Under tini this is what keeps zombies from
    escaping the supervisor when a service double-forks and exits.
    """
    if sys.platform != "linux":
        return False
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        pr_set_child_subreaper = 36
        result = libc.prctl(pr_set_child_subreaper, 1, 0, 0, 0)
        return result == 0
    except Exception:
        return False
