"""Sovereign OS: the environment agents live and work in.

On launch AVRONE establishes this environment first (workspace, runner,
health check) so hard requests never fail on a missing sandbox. Commands run
in a Docker container when Docker is available (the "VM environment"),
otherwise in a local subprocess confined to the workspace directory.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import agentcontainer, agentfs
from .config import Settings


@dataclass
class CommandResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def text(self, limit: int = 6000) -> str:
        out = f"$ {self.cmd}\n[exit {self.exit_code}{' TIMEOUT' if self.timed_out else ''}]\n"
        if self.stdout:
            out += self.stdout
        if self.stderr:
            out += "\n[stderr]\n" + self.stderr
        return out[:limit] + ("\n...[truncated]" if len(out) > limit else "")


class Runner:
    kind = "base"

    def __init__(self, settings: Settings):
        self.s = settings
        self.workspace: Path = settings.workspace

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        raise NotImplementedError

    def _agent_cwd(self, cwd: Path | None) -> Path | None:
        """Validate a per-agent cwd: it must be one confined workspace/agents/<id> dir."""
        if cwd is None:
            return None
        if not agentfs.is_agent_dir(self.workspace, cwd):
            raise agentfs.AgentFSError(f"cwd {cwd} is not a confined agent dir under the workspace")
        return Path(cwd).resolve()

    def agent_env(self, agent_cwd: Path, in_container: str | None = None) -> dict[str, str]:
        return {"AVRONE_AGENT_DIR": in_container or str(agent_cwd),
                "AVRONE_SHARED_WORKSPACE": agentfs.CONTAINER_WORKSPACE if in_container else str(self.workspace.resolve())}

    @staticmethod
    def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        """Stop the command and its children, not just the shell waiting for them."""
        try:
            if os.name == "posix":
                # Do not return early when the shell has exited: background children can
                # still hold inherited pipes open after the group leader's returncode is set.
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        except OSError:
            # Best-effort fallback if the process group could not be signalled.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def _exec(self, argv: list[str], cmd: str, timeout: int, cwd: Path | None = None,
                    env: dict[str, str] | None = None) -> CommandResult:
        spawn_options = {"start_new_session": True} if os.name == "posix" else {}
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd else None, env={**os.environ, **env} if env else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **spawn_options)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return CommandResult(cmd, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
        except asyncio.TimeoutError:
            self._kill_process_group(proc)
            await proc.wait()
            return CommandResult(cmd, -1, "", "command timed out", timed_out=True)
        except asyncio.CancelledError:
            self._kill_process_group(proc)
            await proc.wait()
            raise


class LocalRunner(Runner):
    kind = "local"

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        agent_cwd = self._agent_cwd(cwd)
        if agent_cwd is None:  # non-agent command: unchanged, runs at the workspace root
            return await self._exec(["bash", "-lc", cmd], cmd, timeout or self.s.command_timeout, cwd=self.workspace)
        return await self._exec(["bash", "-lc", cmd], cmd, timeout or self.s.command_timeout, cwd=agent_cwd,
                                env=self.agent_env(agent_cwd))


class DockerRunner(Runner):
    """One long-lived container per AVRONE instance; workspace is bind-mounted at /workspace."""
    kind = "docker"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.container = f"avrone-env-{uuid.uuid4().hex[:8]}"
        self.agent_pool: agentcontainer.AgentContainerPool | None = None  # AVRONE_AGENT_CONTAINER=persistent

    def _pool(self) -> agentcontainer.AgentContainerPool:
        if self.agent_pool is None:
            async def _run(argv: list[str], timeout: int) -> CommandResult:
                return await self._exec(argv, " ".join(argv), timeout)
            self.agent_pool = agentcontainer.AgentContainerPool(_run, self.s.docker_image, self.s.allow_network)
        return self.agent_pool

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        argv = ["docker", "run", "-d", "--rm", "--name", self.container,
                "-v", f"{self.workspace}:/workspace", "-w", "/workspace"]
        if not self.s.allow_network:
            argv += ["--network", "none"]
        argv += [self.s.docker_image, "sleep", "infinity"]
        res = await self._exec(argv, " ".join(argv), 300)
        if res.exit_code != 0:
            raise RuntimeError(f"Could not start environment container: {res.stderr}")

    async def stop(self) -> None:
        if self.agent_pool is not None:
            await self.agent_pool.close()
        await self._exec(["docker", "rm", "-f", self.container], "docker rm", 30)

    def exec_argv(self, cmd: str, agent_cwd: Path | None = None) -> list[str]:
        """argv for one command. Agent commands run in their own dir.

        Default: `docker exec -w /workspace/agents/<id>` in the shared container (the agent dir
        is the same host dir via the /workspace bind mount). With AVRONE_AGENT_DOCKER_ISOLATE=1:
        a throwaway `docker run --rm` that mounts ONLY that agent dir, as `-v <dir>:/agent`."""
        if agent_cwd is None:
            return ["docker", "exec", self.container, "bash", "-lc", cmd]
        if agentfs.docker_isolate():
            mnt = agentfs.CONTAINER_AGENT_MOUNT
            argv = ["docker", "run", "--rm", *agentfs.agent_volume_args(agent_cwd), "-w", mnt,
                    "-e", f"AVRONE_AGENT_DIR={mnt}"]
            # same hardening as the persistent agent home (cap_drop ALL, no-new-privileges,
            # mem/cpu/pids limits, --network none unless AVRONE_ALLOW_NETWORK)
            argv += agentcontainer.hardening_args(self.s.allow_network)
            return argv + [self.s.docker_image, "bash", "-lc", cmd]
        inner = agentfs.container_agent_path(self.workspace, agent_cwd)
        argv = ["docker", "exec", "-w", inner]
        for k, v in self.agent_env(agent_cwd, in_container=inner).items():
            argv += ["-e", f"{k}={v}"]
        return argv + [self.container, "bash", "-lc", cmd]

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        agent_cwd = self._agent_cwd(cwd)
        if agent_cwd is not None and agentcontainer.container_mode() == "persistent":
            # long-lived per-agent home container (only the agent dir mounted at /agent), reused
            name = await self._pool().ensure(agent_cwd)
            return await self._exec(agentcontainer.exec_argv(name, cmd), cmd, timeout or self.s.command_timeout)
        return await self._exec(self.exec_argv(cmd, agent_cwd), cmd, timeout or self.s.command_timeout)


async def establish(settings: Settings) -> Runner:
    """Pick and boot the runner, then health-check it."""
    choice = settings.runner
    if choice == "auto":
        choice = "docker" if shutil.which("docker") else "local"
    runner: Runner = DockerRunner(settings) if choice == "docker" else LocalRunner(settings)
    try:
        await runner.start()
    except Exception:
        if choice == "docker" and settings.runner == "auto":
            runner = LocalRunner(settings)
            await runner.start()
        else:
            raise
    check = await runner.run("echo ready", timeout=60)
    if check.exit_code != 0:
        raise RuntimeError(f"Environment health check failed: {check.text()}")
    return runner
