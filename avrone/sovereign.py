"""Sovereign OS: the environment agents live and work in.

On launch AVRONE establishes this environment first (workspace, runner,
health check) so hard requests never fail on a missing sandbox. Commands run
in a Docker container when Docker is available (the "VM environment"),
otherwise in a local subprocess confined to the workspace directory.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

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

    async def run(self, cmd: str, timeout: int | None = None) -> CommandResult:
        raise NotImplementedError

    async def _exec(self, argv: list[str], cmd: str, timeout: int, cwd: Path | None = None) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return CommandResult(cmd, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(cmd, -1, "", "command timed out", timed_out=True)


class LocalRunner(Runner):
    kind = "local"

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def run(self, cmd: str, timeout: int | None = None) -> CommandResult:
        return await self._exec(["bash", "-lc", cmd], cmd, timeout or self.s.command_timeout, cwd=self.workspace)


class DockerRunner(Runner):
    """One long-lived container per AVRONE instance; workspace is bind-mounted at /workspace."""
    kind = "docker"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.container = f"avrone-env-{uuid.uuid4().hex[:8]}"

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
        await self._exec(["docker", "rm", "-f", self.container], "docker rm", 30)

    async def run(self, cmd: str, timeout: int | None = None) -> CommandResult:
        return await self._exec(["docker", "exec", self.container, "bash", "-lc", cmd], cmd,
                                timeout or self.s.command_timeout)


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
