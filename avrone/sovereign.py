"""Sovereign OS: the environment agents live and work in.

On launch AVRONE establishes this environment first (workspace, runner,
health check) so hard requests never fail on a missing sandbox. Commands run
in a Docker container when Docker is available (the "VM environment"),
otherwise in a local subprocess confined to the workspace directory.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


_SENSITIVE_ENV_PARTS = ("_TOKEN", "_API_KEY", "_SECRET", "_PASSWORD", "_CREDENTIAL", "_PRIVATE_KEY")
_SENSITIVE_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN", "GH_TOKEN", "DOCKER_AUTH_CONFIG", "NPM_TOKEN",
}


def _agent_safe_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child-process environment without application credentials."""
    env = dict(os.environ if source is None else source)
    for key in tuple(env):
        upper = key.upper()
        if upper in _SENSITIVE_ENV_NAMES or any(part in upper for part in _SENSITIVE_ENV_PARTS):
            env.pop(key, None)
    return env


def _docker_image_policy_error(image: str) -> str | None:
    allowlist = {
        item.strip() for item in os.getenv("AVRONE_DOCKER_IMAGE_ALLOWLIST", "").split(",") if item.strip()
    }
    if allowlist and image not in allowlist:
        return "Docker image is not in AVRONE_DOCKER_IMAGE_ALLOWLIST."
    if os.getenv("AVRONE_DOCKER_REQUIRE_DIGEST", "").lower() in {"1", "true", "yes"} and not re.search(
        r"@sha256:[0-9a-f]{64}$", image
    ):
        return "This node requires Docker images pinned by sha256 digest."
    return None


def _docker_workspace_user_args(workspace: Path) -> list[str]:
    """Run workspace-bound agents as the owner of their shared worktree."""
    owner = workspace.stat()
    return ["--user", f"{owner.st_uid}:{owner.st_gid}", "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp"]


def _docker_sandbox_args(*, allow_network: bool) -> list[str]:
    """Return the shared least-privilege/resource policy for agent-owned containers."""
    def bounded_memory(raw: str) -> str:
        value = raw.strip().lower()
        match = re.fullmatch(r"([0-9]{1,5})(m|g)", value)
        if not match:
            return "2g"
        amount = int(match.group(1)) * (1024 if match.group(2) == "g" else 1)
        return value if 128 <= amount <= 65_536 else "2g"

    try:
        cpu = float(os.getenv("AVRONE_DOCKER_CPU_LIMIT", "2"))
    except ValueError:
        cpu = 2.0
    if not 0.1 <= cpu <= 32:
        cpu = 2.0
    try:
        pids = int(os.getenv("AVRONE_DOCKER_PIDS_LIMIT", "512"))
    except ValueError:
        pids = 512
    if not 32 <= pids <= 4096:
        pids = 512

    args = [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit", str(pids),
        "--memory", bounded_memory(os.getenv("AVRONE_DOCKER_MEMORY_LIMIT", "2g")),
        "--cpus", str(cpu),
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
    ]
    if not allow_network:
        args += ["--network", "none"]
    return args


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
            *argv, cwd=str(cwd) if cwd else None, env=_agent_safe_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"))
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return CommandResult(cmd, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
        except asyncio.TimeoutError:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            else:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            return CommandResult(cmd, -1, "", "command timed out; process group terminated", timed_out=True)


class LocalRunner(Runner):
    kind = "local"

    async def start(self) -> None:
        if not self.s.allow_network:
            raise RuntimeError(
                "The local runner cannot enforce AVRONE_ALLOW_NETWORK=false for arbitrary shell commands. "
                "Use AVRONE_RUNNER=docker (which applies --network none) or configure host-level egress rules."
            )
        if hasattr(os, "geteuid") and os.geteuid() == 0 and os.getenv("AVRONE_ALLOW_ROOT_LOCAL", "").lower() not in {"1", "true", "yes"}:
            raise RuntimeError(
                "Refusing to run agent shell commands as host root. Use AVRONE_RUNNER=docker "
                "or run AVRONE under an unprivileged account. AVRONE_ALLOW_ROOT_LOCAL=true "
                "is a dangerous break-glass override for disposable isolated hosts only."
            )
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
        image_error = _docker_image_policy_error(self.s.docker_image)
        if image_error:
            raise RuntimeError(image_error)
        self.workspace.mkdir(parents=True, exist_ok=True)
        argv = ["docker", "run", "-d", "--rm", "--name", self.container,
                *_docker_sandbox_args(allow_network=self.s.allow_network),
                *_docker_workspace_user_args(self.workspace),
                "-v", f"{self.workspace}:/workspace", "-w", "/workspace",
                self.s.docker_image, "sleep", "infinity"]
        res = await self._exec(argv, " ".join(argv), 300)
        if res.exit_code != 0:
            raise RuntimeError(f"Could not start environment container: {res.stderr}")

    async def stop(self) -> None:
        await self._exec(["docker", "rm", "-f", self.container], "docker rm", 30)

    async def run(self, cmd: str, timeout: int | None = None) -> CommandResult:
        limit = max(1, int(timeout or self.s.command_timeout))
        # The container-side timeout kills the command even if the Docker client stays connected.
        result = await self._exec(
            ["docker", "exec", self.container, "timeout", "-s", "KILL", f"{limit}s",
             "bash", "-lc", cmd],
            cmd, limit + 5,
        )
        if result.exit_code in {124, 137} and not result.timed_out:
            return CommandResult(cmd, result.exit_code, result.stdout,
                                result.stderr or "command timed out inside container", timed_out=True)
        return result


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
