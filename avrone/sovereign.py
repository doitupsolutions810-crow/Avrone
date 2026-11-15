"""Sovereign OS: the environment agents live and work in.

On launch AVRONE establishes this environment first (workspace, runner,
health check) so hard requests never fail on a missing sandbox. Commands run
in a Docker container when Docker is available (the "VM environment"),
otherwise in a local subprocess confined to the workspace directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import agentcontainer, agentfs
from .config import Settings


async def _reap(proc) -> None:
    """Kill a child that may already have exited, then wait for it (never raises ProcessLookupError)."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 - reaping is best effort
        pass


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

    async def _exec(self, argv: list[str], cmd: str, timeout: int, cwd: Path | None = None,
                    env: dict[str, str] | None = None) -> CommandResult:
        if any("\x00" in a for a in argv):
            return CommandResult(cmd.replace("\x00", "\\0"), -1, "", "refused: command contains a NUL byte")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd) if cwd else None, env={**os.environ, **env} if env else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as e:  # E2BIG, missing cwd/binary, bad env: a result, not a crash
            return CommandResult(cmd, -1, "", f"could not start command: {type(e).__name__}: {e}"[:500])
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return CommandResult(cmd, proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
        except asyncio.TimeoutError:
            await _reap(proc)
            return CommandResult(cmd, -1, "", "command timed out", timed_out=True)
        except asyncio.CancelledError:
            # a cancelled run (evolve_cancel, client gone, squad shutdown) must not leave the child running
            await asyncio.shield(_reap(proc))
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
        self.container_id: str | None = None
        self.image_id: str | None = None
        self._owner_token = uuid.uuid4().hex
        self.agent_pool: agentcontainer.AgentContainerPool | None = None  # AVRONE_AGENT_CONTAINER=persistent

    def _pool(self) -> agentcontainer.AgentContainerPool:
        if self.agent_pool is None:
            async def _run(argv: list[str], timeout: int) -> CommandResult:
                return await self._exec(argv, " ".join(argv), timeout)
            self.agent_pool = agentcontainer.AgentContainerPool(_run, self.s.docker_image, self.s.allow_network)
        return self.agent_pool

    @staticmethod
    def _full_container_id(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip().lower()
        return candidate if re.fullmatch(r"[0-9a-f]{64}", candidate) else None

    def _inspect_started_container_id(
        self, stdout: str, *, expected_image_id: str, expected_container_id: str | None = None
    ) -> str | None:
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            return None
        if isinstance(payload, list) and len(payload) == 1:
            payload = payload[0]
        if not isinstance(payload, dict):
            return None
        container_id = self._full_container_id(payload.get("Id"))
        config = payload.get("Config")
        host = payload.get("HostConfig")
        state = payload.get("State")
        labels = config.get("Labels") if isinstance(config, dict) else None
        binds = host.get("Binds") if isinstance(host, dict) else None
        mounts = payload.get("Mounts")
        if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(state, dict):
            return None
        if not isinstance(labels, dict) or labels.get("avrone.runner") != self._owner_token:
            return None
        if payload.get("Name") != f"/{self.container}" or state.get("Running") is not True:
            return None
        if (
            config.get("Image") != expected_image_id
            or payload.get("Image") != expected_image_id
            or config.get("WorkingDir") != "/workspace"
        ):
            return None
        if expected_container_id is not None and container_id != expected_container_id:
            return None
        expected_source = str(self.workspace.resolve())
        if not isinstance(binds, list) or len(binds) != 1 or not isinstance(binds[0], str):
            return None
        bind_parts = binds[0].split(":")
        if (
            len(bind_parts) not in (2, 3)
            or os.path.normpath(bind_parts[0]) != os.path.normpath(expected_source)
            or bind_parts[1] != "/workspace"
            or bind_parts[2:] not in ([], ["rw"])
        ):
            return None
        if (
            not isinstance(mounts, list) or len(mounts) != 1
            or not isinstance(mounts[0], dict)
            or mounts[0].get("Type") != "bind"
            or mounts[0].get("Destination") != "/workspace"
            or mounts[0].get("RW") is not True
        ):
            return None
        network_mode = host.get("NetworkMode")
        if not isinstance(network_mode, str):
            return None
        if not self.s.allow_network and network_mode != "none":
            return None
        normalized_network = network_mode.lower()
        if self.s.allow_network and (
            normalized_network in {"host", "container", "service"}
            or normalized_network.startswith(("container:", "service:"))
        ):
            return None
        return container_id

    async def start(self) -> None:
        if self.container_id is not None:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        image_result = await self._exec(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.s.docker_image],
            "docker image inspect runner", 30,
        )
        image_id = str(image_result.stdout or "").strip().lower()
        if image_result.exit_code != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            detail = str(image_result.stderr or "")[:300]
            raise RuntimeError(
                "Could not resolve the Docker runner image to a valid immutable ID"
                + (f": {detail}" if detail else "")
            )
        argv = ["docker", "run", "-d", "--rm", "--name", self.container,
                "--label", f"avrone.runner={self._owner_token}",
                "-v", f"{self.workspace}:/workspace", "-w", "/workspace"]
        if not self.s.allow_network:
            argv += ["--network", "none"]
        argv += [image_id, "sleep", "infinity"]
        res = await self._exec(argv, " ".join(argv), 300)
        if res.exit_code != 0:
            raise RuntimeError(f"Could not start environment container: {res.stderr}")

        returned_id = self._full_container_id(res.stdout)
        if returned_id is not None:
            # Always inspect the exact object ID Docker returned, even when stdout looks
            # syntactically valid. This prevents a wrapper from redirecting the runner to
            # a different container with a valid but unrelated ID.
            inspected = await self._exec(
                ["docker", "inspect", "-f", "{{json .}}", returned_id],
                "docker inspect runner by ID", 30,
            )
            container_id = (
                self._inspect_started_container_id(
                    inspected.stdout, expected_image_id=image_id, expected_container_id=returned_id
                )
                if inspected.exit_code == 0 else None
            )
        else:
            # stdout was truncated or malformed. Recover only from the unique friendly name
            # when inspect proves the unguessable owner label and the exact runner policy.
            inspected = await self._exec(
                ["docker", "inspect", "-f", "{{json .}}", self.container],
                "docker inspect runner by owned name", 30,
            )
            container_id = (
                self._inspect_started_container_id(inspected.stdout, expected_image_id=image_id)
                if inspected.exit_code == 0 else None
            )
        if container_id is None:
            raise RuntimeError(
                "Docker did not return a valid environment container ID and a matching owned container "
                "could not be verified"
            )
        self.image_id = image_id
        self.container_id = container_id

    async def stop(self) -> None:
        pool_error: Exception | None = None
        cancelled: asyncio.CancelledError | None = None
        if self.agent_pool is not None:
            try:
                await self.agent_pool.close()
            except asyncio.CancelledError as exc:
                # The pool shields its internal cleanup task. Even when this waiter is
                # cancelled, still attempt to remove the outer runner container.
                cancelled = exc
            except Exception as exc:  # noqa: BLE001 - clean the outer container regardless
                pool_error = exc

        async def remove_runner_container() -> None:
            container_id = self.container_id
            if container_id is None:
                # Never use the friendly name as a cleanup target when this runner instance
                # has no verified Docker identity.
                return
            result = await self._exec(
                ["docker", "rm", "-f", container_id], "docker rm", 30
            )
            if (result.exit_code != 0
                    and "no such container" not in str(result.stderr or "").lower()
                    and "no such object" not in str(result.stderr or "").lower()):
                raise RuntimeError(
                    f"Could not remove environment container {container_id}: "
                    f"{str(result.stderr or 'unknown Docker error')[:300]}"
                )
            if self.container_id == container_id:
                self.container_id = None

        # Shield the independent outer-container cleanup from caller cancellation. The
        # cancellation is re-raised only after the removal attempt has completed.
        remove_task = asyncio.create_task(remove_runner_container())
        try:
            await asyncio.shield(remove_task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            try:
                await asyncio.shield(remove_task)
            except Exception as cleanup_exc:  # noqa: BLE001
                if pool_error is None:
                    pool_error = cleanup_exc
        except Exception as exc:  # noqa: BLE001
            if pool_error is None:
                pool_error = exc

        if cancelled is not None:
            raise cancelled
        if pool_error is not None:
            raise pool_error

    def exec_argv(
        self, cmd: str, agent_cwd: Path | None = None, *, container_ref: str | None = None
    ) -> list[str]:
        """argv for one command. Agent commands run in their own dir.

        Default: `docker exec -w /workspace/agents/<id>` in the shared container (the agent dir
        is the same host dir via the /workspace bind mount). With AVRONE_AGENT_DOCKER_ISOLATE=1:
        a throwaway `docker run --rm` that mounts ONLY that agent dir, as `-v <dir>:/agent`."""
        target = container_ref or self.container_id or self.container
        if agent_cwd is None:
            return ["docker", "exec", target, "bash", "-lc", cmd]
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
        return argv + [target, "bash", "-lc", cmd]

    async def run(self, cmd: str, timeout: int | None = None, cwd: Path | None = None) -> CommandResult:
        agent_cwd = self._agent_cwd(cwd)
        if agent_cwd is not None and agentcontainer.container_mode() == "persistent":
            # long-lived per-agent home container (only the agent dir mounted at /agent), reused
            pool = self._pool()
            async with pool.lease(agent_cwd) as name:
                # Execute against the immutable Docker ID captured at lease admission. A
                # remove/recreate race on the friendly name must never redirect a command.
                container_id = pool.container_id(name)
                return await self._exec(
                    agentcontainer.exec_argv(container_id, cmd), cmd, timeout or self.s.command_timeout
                )
        target = self.container_id
        if target is None:
            raise RuntimeError("Docker runner must be started and have a verified container ID before shared commands")
        return await self._exec(
            self.exec_argv(cmd, agent_cwd, container_ref=target),
            cmd, timeout or self.s.command_timeout,
        )


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
