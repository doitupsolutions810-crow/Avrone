"""Persistent, per-agent containers ("agent homes") for the Docker runner.

    AVRONE_AGENT_CONTAINER   off | persistent   (default off)

With ``persistent`` and the Docker runner active, every agent that runs a command gets ONE
long-lived, named container

    avrone-agent-<node>-<agent_id>

that mounts ONLY that agent's own dir (``workspace/agents/<id>``) at ``/agent``. It is created
lazily on the agent's first command, reused for every later command and run (state in /agent and
the container's own filesystem survive between commands), and reaped (``docker rm -f``) once it
has been idle for AVRONE_AGENT_IDLE_SECS. It is hardened like a throwaway sandbox:

    --user <non-root host UID:GID>  --read-only  --tmpfs /tmp (128 MB)
    --cap-drop ALL  --security-opt no-new-privileges  --pids-limit 512
    --memory <AVRONE_AGENT_MEM_MB>m (default 512)   --memory-swap equal to memory
    --cpus <AVRONE_AGENT_CPUS> (default 1.0)  --network none unless AVRONE_ALLOW_NETWORK

Every docker argv is built by a PURE function here (``persistent_run_argv`` / ``exec_argv`` /
``inspect_argv`` / ``rm_argv``) so it is unit-testable without Docker. ``AgentContainerPool``
drives them through an injected async ``exec`` callable and an injected clock.

Off (the default) changes nothing: the runner keeps its v0.16 behaviour (shared container, or a
throwaway container per command with AVRONE_AGENT_DOCKER_ISOLATE=1).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import agentfs

DEFAULT_MEM_MB, MIN_MEM_MB, MAX_MEM_MB = 512, 64, 65536
DEFAULT_CPUS, MIN_CPUS, MAX_CPUS = 1.0, 0.1, 64.0
DEFAULT_IDLE_SECS, MIN_IDLE_SECS = 600.0, 5.0
PIDS_LIMIT = 512
NAME_PREFIX = "avrone-agent"
_NAME_BAD = re.compile(r"[^a-z0-9_.-]+")


def container_mode() -> str:
    """'persistent' or 'off' (anything unrecognised is off: fails closed to v0.16 behaviour)."""
    v = (os.environ.get("AVRONE_AGENT_CONTAINER") or "off").strip().lower()
    return "persistent" if v == "persistent" else "off"


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        v = default
    if v != v or v <= 0:  # NaN / non-positive
        v = default
    return max(lo, min(hi, v))


def mem_mb() -> int:
    return int(_num("AVRONE_AGENT_MEM_MB", DEFAULT_MEM_MB, MIN_MEM_MB, MAX_MEM_MB))


def cpus() -> float:
    return round(_num("AVRONE_AGENT_CPUS", DEFAULT_CPUS, MIN_CPUS, MAX_CPUS), 2)


def idle_secs() -> float:
    return _num("AVRONE_AGENT_IDLE_SECS", DEFAULT_IDLE_SECS, MIN_IDLE_SECS, 7 * 86400.0)


def local_node_id() -> str:
    raw = (os.environ.get("AVRONE_NODE_ID") or "").strip() or socket.gethostname()
    return _NAME_BAD.sub("-", raw.lower()).strip("-.")[:40] or "node"


def container_name(node: str, agent_id: str) -> str:
    """Docker-safe, deterministic name for one agent's home container."""
    n = _NAME_BAD.sub("-", str(node).lower()).strip("-.")[:40] or "node"
    return f"{NAME_PREFIX}-{n}-{agentfs.sanitize_agent_id(agent_id)}"


def sandbox_identity() -> tuple[int, int]:
    """Return a non-root UID:GID for agent containers, defaulting to the AVRONE host user.

    Bind-mounted agent homes are created mode 0700 by the host process, so the container
    identity must match their owner. Explicit overrides are useful only when the workspace
    directories are already owned by that UID:GID. Root is rejected: a root host process
    must explicitly arrange a non-root-owned workspace rather than silently running agents
    as root inside containers.
    """
    def read_id(name: str, fallback: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        try:
            value = int(raw) if raw else int(fallback)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a positive integer") from exc
        if value <= 0 or value > 2_147_483_647:
            raise RuntimeError(f"{name} must be a positive non-root integer")
        return value

    uid_fallback = os.getuid() if hasattr(os, "getuid") else 1000
    gid_fallback = os.getgid() if hasattr(os, "getgid") else 1000
    return read_id("AVRONE_AGENT_UID", uid_fallback), read_id("AVRONE_AGENT_GID", gid_fallback)


def hardening_args(allow_network: bool, mem: int | None = None, cpu: float | None = None) -> list[str]:
    """Security boundary shared by persistent and one-shot agent containers.

    Containers run as the non-root host owner of their private 0700 home. The image root
    filesystem is read-only; only /agent and a bounded /tmp tmpfs are writable. Capabilities
    and privilege escalation are disabled, swap is capped at the memory limit, and PIDs,
    CPU and RAM are bounded. Agent networking is opt-in and should normally remain disabled.
    """
    uid, gid = sandbox_identity()
    if mem is None:
        memory = mem_mb()
    else:
        if type(mem) is not int or mem <= 0:
            raise ValueError("mem must be a positive integer in MB")
        memory = max(MIN_MEM_MB, min(MAX_MEM_MB, mem))
    if cpu is None:
        cpu_limit = cpus()
    else:
        if isinstance(cpu, bool):
            raise ValueError("cpu must be a positive finite number")
        try:
            cpu_value = float(cpu)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("cpu must be a positive finite number") from exc
        if not math.isfinite(cpu_value) or cpu_value <= 0:
            raise ValueError("cpu must be a positive finite number")
        cpu_limit = round(max(MIN_CPUS, min(MAX_CPUS, cpu_value)), 2)
    args = [
        "--user", f"{uid}:{gid}",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
        "--env", "HOME=/agent",
        "--env", "TMPDIR=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", f"{memory}m",
        "--memory-swap", f"{memory}m",
        "--cpus", str(cpu_limit),
        "--ulimit", "nofile=1024:2048",
    ]
    if not allow_network:
        args += ["--network", "none"]
    return args


def persistent_run_argv(name: str, host_dir: Path, image: str, allow_network: bool,
                        mem: int | None = None, cpu: float | None = None, agent_id: str = "") -> list[str]:
    """`docker run -d` for a long-lived agent home: ONLY the agent dir is mounted, at /agent."""
    mnt = agentfs.CONTAINER_AGENT_MOUNT
    labels = ["--label", "avrone.agent-home=1"]
    if agent_id:
        labels += ["--label", f"avrone.agent={agentfs.sanitize_agent_id(agent_id)}"]
    return ["docker", "run", "-d", "--name", name, *labels,
            *hardening_args(allow_network, mem, cpu),
            *agentfs.agent_volume_args(host_dir), "-w", mnt, "-e", f"AVRONE_AGENT_DIR={mnt}",
            image, "sleep", "infinity"]


def exec_argv(name: str, cmd: str) -> list[str]:
    mnt = agentfs.CONTAINER_AGENT_MOUNT
    return ["docker", "exec", "-w", mnt, "-e", f"AVRONE_AGENT_DIR={mnt}", name, "bash", "-lc", cmd]


def inspect_argv(name: str) -> list[str]:
    # A bare .State.Running check is not enough to adopt a named container: another process
    # could leave a running but privileged/host-network/shared-volume container under our name.
    return ["docker", "inspect", "-f", "{{json .}}", name]


def _inspect_matches_policy(stdout: str, agent_dir: Path, image: str,
                            allow_network: bool) -> tuple[bool, bool, str]:
    """Return (compatible, AVRONE-owned, reason) for a docker inspect JSON object.

    Docker Desktop can translate a macOS path in Mounts.Source into a VM path. HostConfig.Binds
    preserves the requested bind specification, while Mounts proves only one writable /agent
    bind is actually attached.
    """
    try:
        payload = json.loads(str(stdout or ""))
    except (TypeError, ValueError):
        return False, False, "inspect output is not JSON"
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        return False, False, "inspect output is not one container object"

    config = payload.get("Config") if isinstance(payload.get("Config"), dict) else {}
    host = payload.get("HostConfig") if isinstance(payload.get("HostConfig"), dict) else {}
    state = payload.get("State") if isinstance(payload.get("State"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    expected_agent = agentfs.sanitize_agent_id(Path(agent_dir).name)
    owned = labels.get("avrone.agent-home") == "1" and labels.get("avrone.agent") == expected_agent
    if not owned:
        return False, False, "container name is occupied by a container not owned by this agent home"

    problems: list[str] = []
    if state.get("Running") is not True:
        problems.append("container is not running")
    uid, gid = sandbox_identity()
    if config.get("User") != f"{uid}:{gid}":
        problems.append("container user does not match the non-root agent identity")
    if config.get("Image") != image:
        problems.append("container image does not match the configured image")
    if config.get("WorkingDir") != agentfs.CONTAINER_AGENT_MOUNT:
        problems.append("working directory is not /agent")
    env = set(config.get("Env") or [])
    required_env = {"HOME=/agent", "TMPDIR=/tmp", "PYTHONDONTWRITEBYTECODE=1",
                    f"AVRONE_AGENT_DIR={agentfs.CONTAINER_AGENT_MOUNT}"}
    if not required_env.issubset(env):
        problems.append("required private-home environment is missing")

    binds = host.get("Binds") or []
    expected_source = str(Path(agent_dir).resolve())
    good_bind = False
    if isinstance(binds, list) and len(binds) == 1 and isinstance(binds[0], str):
        parts = binds[0].split(":")
        good_bind = len(parts) >= 2 and os.path.normpath(parts[0]) == os.path.normpath(expected_source) \\
            and parts[1] == agentfs.CONTAINER_AGENT_MOUNT
    mounts = payload.get("Mounts") or []
    good_mount = (
        isinstance(mounts, list) and len(mounts) == 1
        and isinstance(mounts[0], dict)
        and mounts[0].get("Type") == "bind"
        and mounts[0].get("Destination") == agentfs.CONTAINER_AGENT_MOUNT
        and mounts[0].get("RW") is True
    )
    if not good_bind or not good_mount:
        problems.append("container does not have exactly the expected private /agent bind mount")

    memory_bytes = mem_mb() * 1024 * 1024
    expected_nano_cpus = int(round(cpus() * 1_000_000_000))
    if host.get("ReadonlyRootfs") is not True:
        problems.append("root filesystem is writable")
    tmpfs = host.get("Tmpfs") if isinstance(host.get("Tmpfs"), dict) else {}
    tmpfs_spec = str(tmpfs.get("/tmp", "")).lower()
    if not all(part in tmpfs_spec for part in ("rw", "nosuid", "nodev", "size=128m")):
        problems.append("bounded /tmp tmpfs is missing")
    if "ALL" not in (host.get("CapDrop") or []) or host.get("CapAdd"):
        problems.append("Linux capabilities are not fully dropped")
    if not any("no-new-privileges" in str(item).lower() for item in (host.get("SecurityOpt") or [])):
        problems.append("no-new-privileges is not enabled")
    if not allow_network and host.get("NetworkMode") != "none":
        problems.append("agent container has network access")
    network_mode = str(host.get("NetworkMode", "")).lower()
    if allow_network and network_mode.startswith(("host", "container")):
        problems.append("agent container shares a host/peer network namespace")
    if host.get("Privileged") is not False:
        problems.append("privileged container mode is enabled")
    if host.get("PortBindings"):
        problems.append("container publishes ports")
    if host.get("Devices") or host.get("DeviceRequests"):
        problems.append("container has device access")
    if str(host.get("PidMode") or "").startswith(("host", "container")):
        problems.append("container shares a PID namespace")
    if str(host.get("IpcMode") or "").startswith(("host", "container")):
        problems.append("container shares an IPC namespace")
    if host.get("AutoRemove") is True:
        problems.append("persistent agent container is configured for auto-remove")
    if host.get("Memory") != memory_bytes or host.get("MemorySwap") != memory_bytes:
        problems.append("memory or swap limit does not match policy")
    if host.get("NanoCpus") != expected_nano_cpus:
        problems.append("CPU limit does not match policy")
    if host.get("PidsLimit") != PIDS_LIMIT:
        problems.append("PID limit does not match policy")

    return not problems, True, "; ".join(problems) if problems else "compatible"


def _docker_not_found(result: Any) -> bool:
    detail = str(getattr(result, "stderr", "") or "").lower()
    return "no such object" in detail or "no such container" in detail

def rm_argv(name: str) -> list[str]:
    return ["docker", "rm", "-f", name]


Exec = Callable[[list[str], int], Awaitable[Any]]  # (argv, timeout) -> CommandResult-like


@dataclass
class _Home:
    name: str
    agent_dir: Path
    last_used: float
    created: float = 0.0
    uses: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class AgentContainerPool:
    """Lazily creates, reuses and idle-reaps one container per agent dir."""

    def __init__(self, exec_fn: Exec, image: str, allow_network: bool, node: str | None = None,
                 clock: Callable[[], float] = time.monotonic, idle: float | None = None):
        self._exec = exec_fn
        self.image = image
        self.allow_network = allow_network
        self.node = node or local_node_id()
        self.clock = clock
        self._idle = idle
        self.homes: dict[str, _Home] = {}
        self._lock = asyncio.Lock()
        self.created = 0
        self.reaped = 0

    @property
    def idle_after(self) -> float:
        if self._idle is None:
            return idle_secs()
        if isinstance(self._idle, bool):
            raise ValueError("idle must be a positive finite number of seconds")
        try:
            value = float(self._idle)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("idle must be a positive finite number of seconds") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("idle must be a positive finite number of seconds")
        return max(MIN_IDLE_SECS, min(7 * 86400.0, value))

    def run_argv(self, agent_dir: Path) -> list[str]:
        d = Path(agent_dir).resolve()
        return persistent_run_argv(container_name(self.node, d.name), d, self.image, self.allow_network,
                                   agent_id=d.name)

    async def ensure(self, agent_dir: Path) -> str:
        """Name of a running, policy-compliant home container for agent_dir."""
        d = Path(agent_dir).resolve()
        name = container_name(self.node, d.name)
        await self.reap_idle(skip=name)
        async with self._lock:
            now = self.clock()
            home = self.homes.get(name)
            if home is not None:
                home.last_used, home.uses = now, home.uses + 1
                return name

            inspected = await self._exec(inspect_argv(name), 30)
            code = getattr(inspected, "exit_code", 1)
            compatible = False
            if code == 0:
                compatible, owned, reason = _inspect_matches_policy(
                    str(getattr(inspected, "stdout", "")), d, self.image, self.allow_network
                )
                if not owned:
                    # A colliding name is not permission to kill or adopt an arbitrary container.
                    raise RuntimeError(f"refusing to adopt unowned agent container {name}: {reason}")
                if not compatible:
                    removed = await self._exec(rm_argv(name), 30)
                    if getattr(removed, "exit_code", 1) != 0:
                        raise RuntimeError(f"could not remove non-compliant agent container {name}")
            elif _docker_not_found(inspected):
                # Keep stale-container cleanup; rm on an absent name is harmless.
                await self._exec(rm_argv(name), 30)
            else:
                detail = str(getattr(inspected, "stderr", "") or "")[:300]
                raise RuntimeError(f"could not inspect agent container {name}: {detail or 'unknown Docker error'}")

            if code != 0 or not compatible:
                created = await self._exec(self.run_argv(d), 300)
                if getattr(created, "exit_code", 1) != 0:
                    raise RuntimeError(f"could not start agent container {name}: "
                                       f"{str(getattr(created, 'stderr', ''))[:300]}")
                # Validate the object Docker actually started, not just the argv AVRONE intended
                # to submit. Custom images can exit immediately and daemons can normalize options.
                verify = await self._exec(inspect_argv(name), 30)
                verified, _owned, reason = _inspect_matches_policy(
                    str(getattr(verify, "stdout", "")), d, self.image, self.allow_network
                ) if getattr(verify, "exit_code", 1) == 0 else (False, False, "container not found after start")
                if not verified:
                    await self._exec(rm_argv(name), 30)
                    raise RuntimeError(f"new agent container {name} failed sandbox verification: {reason}")
                self.created += 1

            self.homes[name] = _Home(name, d, now, created=now, uses=1)
            return name

    async def reap_idle(self, now: float | None = None, skip: str = "") -> list[str]:
        """Remove every home container idle for longer than AVRONE_AGENT_IDLE_SECS."""
        now = self.clock() if now is None else now
        limit = self.idle_after
        # The stale decision and the `docker rm` are one critical section. The rm used to run after
        # the lock was released: a concurrent ensure() for that same agent then found no home
        # record, inspected the still-running container, re-registered and handed it out, and the
        # reaper's rm -f killed it under the agent (its next exec failed / its state was lost).
        async with self._lock:
            stale = [h for n, h in self.homes.items() if n != skip and now - h.last_used >= limit]
            for h in stale:
                del self.homes[h.name]
            for h in stale:
                try:
                    await self._exec(rm_argv(h.name), 30)
                    self.reaped += 1
                except Exception:  # noqa: BLE001 - a failed rm never breaks a command
                    pass
        return [h.name for h in stale]

    async def close(self) -> None:
        async with self._lock:  # same as reap_idle: no ensure() can re-adopt a home mid-removal
            names = list(self.homes)
            self.homes.clear()
            for n in names:
                try:
                    await self._exec(rm_argv(n), 30)
                except Exception:  # noqa: BLE001
                    pass

    def state(self) -> dict[str, Any]:
        now = self.clock()
        return {"mode": "persistent", "node": self.node, "idle_secs": self.idle_after,
                "created": self.created, "reaped": self.reaped,
                "homes": {n: {"agent": h.agent_dir.name, "uses": h.uses, "idle_s": round(now - h.last_used, 1)}
                          for n, h in self.homes.items()}}
