"""Internal builder SDK over AVRONE's existing TrinityCore and Ollama gateway."""
from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .bootstrap import build_core
from .config import Settings

BuildMode = Literal["squad", "chat", "deep"]
BuildStatus = Literal["UNVERIFIED", "VERIFIED", "PASS", "FAIL", "NOT_RUN"]
_GIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def _full_git_sha(value: Any) -> bool:
    return isinstance(value, str) and _GIT_SHA.fullmatch(value) is not None

CONTROL_ROOM_SPEC = """
Build the AVRONE Control Room in the existing repository and harness. Reuse
TrinityCore PLAN/SQUAD/LEAD, Eternal CodeX, Sovereign OS, Ollama through the
existing OpenAI-compatible Gateway, agent workspaces and checkpoints.
Implement repository/PR/commit/CI provenance, durable resumable missions,
agent/runner/container/lease/heartbeat visibility, verification evidence,
release gates and audit history. Distinguish VERIFIED, UNVERIFIED, BLOCKED,
NOT RUN, PASS, FAIL, OPEN, DRAFT, MERGED, CLOSED, UNMERGED, STALE and EXPIRED.
Missing evidence is never PASS; agent claims are not independent verification.
Do not auto-merge, push to protected branches, publish, provision cloud resources,
or claim integrations/runners are active without observed evidence. Work in an
isolated feature branch and leave a human-reviewable diff.
""".strip()


@dataclass(frozen=True)
class BuilderPolicy:
    require_ollama: bool = True
    max_task_chars: int = 100_000
    max_workers: int = 6
    max_deep_rounds: int = 2
    verification_timeout_s: int = 1800
    verification_commands: tuple[str, ...] = (
        "python -m compileall -q avrone",
        "python -m pytest -q",
    )
    require_git_sha_for_verified: bool = True
    # Optional exact workspace revision. When set, verification refuses to run on any other SHA.
    expected_sha: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("require_ollama", self.require_ollama),
            ("require_git_sha_for_verified", self.require_git_sha_for_verified),
        ):
            if type(value) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name, value in (
            ("max_task_chars", self.max_task_chars),
            ("max_workers", self.max_workers),
            ("max_deep_rounds", self.max_deep_rounds),
            ("verification_timeout_s", self.verification_timeout_s),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.verification_commands, tuple) or any(
            not isinstance(command, str) or not command.strip()
            for command in self.verification_commands
        ):
            raise ValueError("verification_commands must be a tuple of non-empty strings")
        if self.expected_sha is not None and (
            not isinstance(self.expected_sha, str)
            or not _full_git_sha(self.expected_sha)
        ):
            raise ValueError("expected_sha must be a full 40- or 64-character Git SHA")


@dataclass
class VerificationEvidence:
    status: BuildStatus = "NOT_RUN"
    target_sha: str | None = None
    workspace_clean: bool | None = None
    commands: list[dict[str, Any]] = field(default_factory=list)
    reason: str = "Verification has not run."
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class BuildResult:
    run_id: str
    mode: BuildMode
    status: BuildStatus
    answer: str
    raw: dict[str, Any] = field(default_factory=dict)
    evidence: VerificationEvidence = field(default_factory=VerificationEvidence)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BuilderAgentSDK:
    """A thin, policy-bound facade; Trinity remains the execution engine."""

    def __init__(self, core: Any, cleanups: list[Any] | None = None,
                 policy: BuilderPolicy | None = None):
        self.core = core
        self.cleanups = cleanups or []
        self.policy = policy or BuilderPolicy()
        self.closed = False
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(cls, settings: Settings | None = None, *,
                   policy: BuilderPolicy | None = None,
                   extras: bool = True) -> "BuilderAgentSDK":
        if type(extras) is not bool:
            raise ValueError("extras must be a boolean")
        policy = policy or BuilderPolicy()
        settings = settings or Settings()
        if policy.require_ollama and str(settings.provider).lower() != "ollama":
            raise RuntimeError(
                "Builder SDK requires AVRONE_PROVIDER=ollama; "
                f"configured provider is {settings.provider!r}. No build was started."
            )
        if not str(settings.llm_model or "").strip():
            raise RuntimeError("Ollama model is not configured; no build was started.")
        core, cleanups = await build_core(settings, extras=extras)
        return cls(core, cleanups, policy)

    async def _finish_close(self) -> None:
        failed: list[Any] = []
        failures: list[BaseException] = []
        for cleanup in reversed(self.cleanups):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except (asyncio.CancelledError, Exception) as exc:
                failed.append(cleanup)
                failures.append(exc)

        if failures:
            # Successful callbacks are consumed exactly once; retain only failed callbacks
            # so a later close() retries unfinished resources without double-closing others.
            self.cleanups = list(reversed(failed))
            self._closing = False
            first = failures[0]
            if isinstance(first, asyncio.CancelledError):
                raise first
            raise RuntimeError(
                f"{len(failures)} cleanup callback(s) failed; close may be retried"
            ) from first

        self.cleanups.clear()
        self.closed = True
        self._closing = False

    async def close(self) -> None:
        if self.closed:
            return
        if self._close_task is None or (self._close_task.done() and not self.closed):
            self._closing = True
            self._close_task = asyncio.create_task(self._finish_close())
        # Cancellation of a caller must not cancel runner/plugin/MCP cleanup.
        # Concurrent callers all await the same task; failed cleanup can be retried.
        await asyncio.shield(self._close_task)

    async def __aenter__(self) -> "BuilderAgentSDK":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _task(self, task: str, context: str | None = None) -> str:
        task = str(task or "").strip()
        if not task:
            raise ValueError("task must be non-empty")
        if len(task) > self.policy.max_task_chars:
            raise ValueError("task exceeds configured size limit")
        parts = [
            "You are AVRONE's internal Builder Agent. Follow this project contract.",
            CONTROL_ROOM_SPEC,
            "OPERATOR TASK:",
            task,
        ]
        if context:
            parts.extend(["ADDITIONAL CONTEXT:", str(context)[:20_000]])
        parts.append(
            "Inspect before changing. Add tests. Never invent tool output. "
            "Report exact commands and results. Leave merge and publication to a human."
        )
        return "\n\n".join(parts)

    async def build(self, task: str, *, mode: BuildMode = "squad",
                    workers: int | None = None, run_id: str | None = None,
                    checkpoint: Any = None, dispatch: str | None = None,
                    rounds: int | None = None, context: str | None = None) -> BuildResult:
        if self.closed or self._closing:
            raise RuntimeError("Builder SDK is closed or closing")
        if mode not in ("squad", "chat", "deep"):
            raise ValueError("mode must be squad, chat, or deep")
        if workers is not None and (
            type(workers) is not int or not 1 <= workers <= self.policy.max_workers
        ):
            raise ValueError("workers must be an integer within configured policy")
        if rounds is not None and (
            type(rounds) is not int or not 1 <= rounds <= self.policy.max_deep_rounds
        ):
            raise ValueError("rounds must be an integer within configured policy")
        if run_id is not None and not isinstance(run_id, str):
            raise ValueError("run_id must be a string")
        rid = run_id or "builder-" + uuid.uuid4().hex[:12]
        if rid in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", rid):
            raise ValueError("invalid run_id")
        prompt = self._task(task, context)
        if mode == "chat":
            raw = await self.core.chat(prompt, run_id=rid)
        elif mode == "deep":
            raw = await self.core.deep(prompt, n=workers, run_id=rid,
                                       rounds=rounds or 1, checkpoint=checkpoint)
        else:
            raw = await self.core.squad(prompt, n=workers, run_id=rid,
                                        checkpoint=checkpoint, dispatch=dispatch)
        if isinstance(raw, dict):
            actual_id = str(raw.get("run_id") or rid)
            answer = str(raw.get("answer") or raw.get("result") or "")
            payload = raw
        else:
            actual_id, answer, payload = rid, str(raw or ""), {"result": raw}
        return BuildResult(actual_id, mode, "UNVERIFIED", answer, payload)

    async def _git_status_porcelain(self) -> str:
        """Read tracked and untracked worktree changes; a commit SHA alone is not a snapshot."""
        status = await self.core.runner.run(
            "git status --porcelain=v1 --untracked-files=all", timeout=20
        )
        exit_code = getattr(status, "exit_code", 1)
        timed_out = getattr(status, "timed_out", None)
        stdout = getattr(status, "stdout", None)
        if (
            type(exit_code) is not int
            or exit_code != 0
            or type(timed_out) is not bool
            or timed_out
            or not isinstance(stdout, str)
        ):
            raise RuntimeError("git status could not produce a trustworthy worktree snapshot")
        return stdout

    async def verify(self, result: BuildResult, *,
                     commands: tuple[str, ...] | None = None) -> VerificationEvidence:
        if self.closed or self._closing:
            raise RuntimeError("Builder SDK is closed or closing")
        if commands is None:
            selected = self.policy.verification_commands
        else:
            if isinstance(commands, (str, bytes)) or not isinstance(commands, (tuple, list)):
                raise ValueError("verification commands must be a tuple or list")
            selected = tuple(commands)
        allowed = set(self.policy.verification_commands)
        if any(not isinstance(command, str) or command not in allowed for command in selected):
            raise ValueError("verification command is not operator-allowlisted")
        if len(set(selected)) != len(selected):
            raise ValueError("verification commands must not contain duplicates")
        evidence = VerificationEvidence(started_at=time.time())
        if not selected:
            evidence.reason = "No verification commands configured."
            evidence.finished_at = time.time()
            result.evidence, result.status = evidence, "NOT_RUN"
            return evidence
        sha_error: str | None = None
        try:
            sha_result = await self.core.runner.run("git rev-parse --verify HEAD", timeout=20)
            sha_raw = getattr(sha_result, "stdout", "")
            sha = sha_raw.strip() if isinstance(sha_raw, str) else ""
            sha_exit = getattr(sha_result, "exit_code", 1)
            sha_timed_out = getattr(sha_result, "timed_out", None)
            if (
                type(sha_exit) is int
                and sha_exit == 0
                and type(sha_timed_out) is bool
                and not sha_timed_out
                and _full_git_sha(sha)
            ):
                evidence.target_sha = sha.lower()
            else:
                sha_error = "initial workspace SHA was unavailable, timed out, or malformed"
        except Exception as exc:
            sha_error = f"initial workspace SHA read failed ({type(exc).__name__}: {exc})"[:500]
        expected_sha = self.policy.expected_sha.lower() if self.policy.expected_sha else None
        if expected_sha and evidence.target_sha != expected_sha:
            evidence.status = "FAIL"
            actual = evidence.target_sha or "unavailable"
            evidence.reason = (
                f"Workspace SHA mismatch: expected {expected_sha}, got {actual}; "
                "no verification commands were run."
            )
            evidence.finished_at = time.time()
            result.evidence, result.status = evidence, "FAIL"
            return evidence

        # A Git commit SHA does not attest uncommitted tracked or untracked files.
        # Refuse to label a dirty checkout VERIFIED even when HEAD matches expected_sha.
        if evidence.target_sha:
            try:
                initial_status = await self._git_status_porcelain()
            except Exception as exc:
                evidence.status = "FAIL"
                evidence.workspace_clean = None
                evidence.reason = (
                    "Could not inspect the worktree before verification; no checks were run "
                    f"({type(exc).__name__}: {exc})."
                )
                evidence.finished_at = time.time()
                result.evidence, result.status = evidence, "FAIL"
                return evidence
            if initial_status.strip():
                evidence.status = "FAIL"
                evidence.workspace_clean = False
                evidence.reason = (
                    "Workspace is dirty (tracked or untracked changes); "
                    "no verification commands were run."
                )
                evidence.finished_at = time.time()
                result.evidence, result.status = evidence, "FAIL"
                return evidence
            evidence.workspace_clean = True

        for command in selected:
            started = time.time()
            try:
                r = await self.core.runner.run(command, timeout=self.policy.verification_timeout_s)
                evidence.commands.append({
                    "command": command,
                    "exit_code": (getattr(r, "exit_code", -1)
                                  if type(getattr(r, "exit_code", -1)) is int else -1),
                    "timed_out": (getattr(r, "timed_out", False)
                                  if isinstance(getattr(r, "timed_out", False), bool) else True),
                    "stdout": str(getattr(r, "stdout", ""))[-12000:],
                    "stderr": str(getattr(r, "stderr", ""))[-12000:],
                    "duration_s": round(time.time() - started, 3),
                })
            except Exception as exc:
                evidence.commands.append({
                    "command": command, "exit_code": -1,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
        # Bind the result to one immutable workspace revision. A concurrent checkout,
        # reset, or branch move while checks execute invalidates the verification.
        if evidence.target_sha:
            try:
                final_sha_result = await self.core.runner.run(
                    "git rev-parse --verify HEAD", timeout=20
                )
                final_sha_raw = getattr(final_sha_result, "stdout", "")
                final_sha = final_sha_raw.strip() if isinstance(final_sha_raw, str) else ""
                final_sha_exit = getattr(final_sha_result, "exit_code", 1)
                final_sha_timed_out = getattr(final_sha_result, "timed_out", None)
                final_sha_ok = (
                    type(final_sha_exit) is int
                    and final_sha_exit == 0
                    and type(final_sha_timed_out) is bool
                    and not final_sha_timed_out
                    and _full_git_sha(final_sha)
                )
            except Exception as exc:
                final_sha, final_sha_ok = "", False
                sha_error = type(exc).__name__
            else:
                sha_error = "unavailable"
            if not final_sha_ok or final_sha.lower() != evidence.target_sha:
                evidence.status = "FAIL"
                if final_sha_ok:
                    evidence.reason = (
                        "Workspace SHA changed during verification: "
                        f"{evidence.target_sha} -> {final_sha.lower()}."
                    )
                else:
                    evidence.reason = (
                        "Could not confirm the workspace SHA remained stable after "
                        f"verification ({sha_error})."
                    )
                evidence.finished_at = time.time()
                result.evidence, result.status = evidence, "FAIL"
                return evidence

            try:
                final_status = await self._git_status_porcelain()
            except Exception as exc:
                evidence.status = "FAIL"
                evidence.workspace_clean = None
                evidence.reason = (
                    "Could not confirm the worktree remained clean after verification "
                    f"({type(exc).__name__}: {exc})."
                )
                evidence.finished_at = time.time()
                result.evidence, result.status = evidence, "FAIL"
                return evidence
            if final_status.strip():
                evidence.status = "FAIL"
                evidence.workspace_clean = False
                evidence.reason = (
                    "Workspace became dirty during verification; checks cannot certify "
                    "a clean exact-SHA source tree."
                )
                evidence.finished_at = time.time()
                result.evidence, result.status = evidence, "FAIL"
                return evidence
            evidence.workspace_clean = True

        failed = [item for item in evidence.commands
                  if item.get("exit_code") != 0 or item.get("timed_out")]
        if failed:
            evidence.status = "FAIL"
            evidence.reason = f"{len(failed)} of {len(evidence.commands)} checks failed."
        elif self.policy.require_git_sha_for_verified and not evidence.target_sha:
            evidence.status = "PASS"
            evidence.reason = (
                "Checks passed, but no exact Git SHA was found; result remains unverified."
                + (f" {sha_error}." if sha_error else "")
            )
        else:
            evidence.status = "PASS"
            evidence.reason = "All allowlisted checks exited successfully."
        evidence.finished_at = time.time()
        result.evidence = evidence
        result.status = (
            "FAIL" if evidence.status == "FAIL"
            else "UNVERIFIED" if self.policy.require_git_sha_for_verified and not evidence.target_sha
            else "VERIFIED"
        )
        return evidence


async def build_control_room(*, mode: BuildMode = "squad", workers: int = 4,
                             run_id: str | None = None,
                             checkpoint: Any = None) -> BuildResult:
    """Run the Control Room implementation mission through the configured Ollama harness."""
    async with await BuilderAgentSDK.open() as builder:
        return await builder.build(
            "Implement the AVRONE Control Room: inspect the repository, plan a "
            "minimal vertical slice, add durable mission state and truthful GitHub "
            "provenance, expose agent/runner status and audit history, add independent "
            "tests, and leave a human-reviewable feature-branch diff. Do not merge "
            "or publish.",
            mode=mode, workers=workers, run_id=run_id, checkpoint=checkpoint,
        )
