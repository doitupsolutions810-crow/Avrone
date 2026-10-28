"""Persistent, per-agent containers ("agent homes") for the Docker runner.

    AVRONE_AGENT_CONTAINER   off | persistent   (default off)

With ``persistent`` and the Docker runner active, every agent that runs a command gets ONE
long-lived, named container

    avrone-agent-<node>-<agent_id>

that mounts ONLY that agent's own dir (``workspace/agents/<id>``) at ``/agent``. It is created
lazily on the agent's first command, reused for every later command and run (state in /agent and
the container's own filesystem survive between commands), and reaped (``docker rm -f``) once it
has been idle for AVRONE_AGENT_IDLE_SECS. It is hardened like a throwaway sandbox:

    --cap-drop ALL  --security-opt no-new-privileges  --pids-limit 512
    --memory <AVRONE_AGENT_MEM_MB>m (default 512)   --cpus <AVRONE_AGENT_CPUS> (default 1.0)
    --network none unless AVRONE_ALLOW_NETWORK

Every docker argv is built by a PURE function here (``persistent_run_argv`` / ``exec_argv`` /
``inspect_argv`` / ``rm_argv``) so it is unit-testable without Docker. ``AgentContainerPool``
drives them through an injected async ``exec`` callable and an injected clock.

Off (the default) changes nothing: the runner keeps its v0.16 behaviour (shared container, or a
throwaway container per command with AVRONE_AGENT_DOCKER_ISOLATE=1).
"""
from __future__ import annotations

import asyncio
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


def hardening_args(allow_network: bool, mem: int | None = None, cpu: float | None = None) -> list[str]:
    """Flags every agent home container runs with (no capabilities, no privilege escalation,
    bounded memory/cpu/pids, no network unless allowed)."""
    args = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(PIDS_LIMIT),
            "--memory", f"{mem_mb() if mem is None else int(mem)}m",
            "--cpus", str(cpus() if cpu is None else cpu)]
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
    return ["docker", "inspect", "-f", "{{.State.Running}}", name]


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
        return idle_secs() if self._idle is None else float(self._idle)

    def run_argv(self, agent_dir: Path) -> list[str]:
        d = Path(agent_dir).resolve()
        return persistent_run_argv(container_name(self.node, d.name), d, self.image, self.allow_network,
                                   agent_id=d.name)

    async def ensure(self, agent_dir: Path) -> str:
        """Name of a running home container for `agent_dir`, creating it if needed."""
        d = Path(agent_dir).resolve()
        name = container_name(self.node, d.name)
        await self.reap_idle(skip=name)
        async with self._lock:
            now = self.clock()
            home = self.homes.get(name)
            if home is not None:
                home.last_used, home.uses = now, home.uses + 1
                return name
            res = await self._exec(inspect_argv(name), 30)
            running = getattr(res, "exit_code", 1) == 0 and "true" in str(getattr(res, "stdout", "")).lower()
            if not running:
                await self._exec(rm_argv(name), 30)  # a stopped leftover with that name; ignore errors
                res = await self._exec(self.run_argv(d), 300)
                if getattr(res, "exit_code", 1) != 0:
                    raise RuntimeError(f"could not start agent container {name}: "
                                       f"{str(getattr(res, 'stderr', ''))[:300]}")
                self.created += 1
            self.homes[name] = _Home(name, d, now, created=now, uses=1)
            return name

    async def reap_idle(self, now: float | None = None, skip: str = "") -> list[str]:
        """Remove every home container idle for longer than AVRONE_AGENT_IDLE_SECS."""
        now = self.clock() if now is None else now
        limit = self.idle_after
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
        async with self._lock:
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
