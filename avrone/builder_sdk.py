"""Internal builder SDK over AVRONE's existing TrinityCore and Ollama gateway."""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .bootstrap import build_core, run_cleanups
from .config import Settings

BuildMode = Literal["squad", "chat", "deep"]
BuildStatus = Literal["UNVERIFIED", "VERIFIED", "PASS", "FAIL", "NOT_RUN"]

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
        if self.expected_sha is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", self.expected_sha):
            raise ValueError("expected_sha must be a full 40-64 character Git SHA")


@dataclass
class VerificationEvidence:
    status: BuildStatus = "NOT_RUN"
    target_sha: str | None = None
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

    @classmethod
    async def open(cls, settings: Settings | None = None, *,
                   policy: BuilderPolicy | None = None,
                   extras: bool = True) -> "BuilderAgentSDK":
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

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await run_cleanups(self.cleanups)

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
        if self.closed:
            raise RuntimeError("Builder SDK is closed")
        if mode not in ("squad", "chat", "deep"):
            raise ValueError("mode must be squad, chat, or deep")
        if workers is not None and (isinstance(workers, bool) or not 1 <= int(workers) <= self.policy.max_workers):
            raise ValueError("workers outside configured policy")
        if rounds is not None and (isinstance(rounds, bool) or not 1 <= int(rounds) <= self.policy.max_deep_rounds):
            raise ValueError("rounds outside configured policy")
        rid = run_id or "builder-" + uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", rid):
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

    async def verify(self, result: BuildResult, *,
                     commands: tuple[str, ...] | None = None) -> VerificationEvidence:
        if self.closed:
            raise RuntimeError("Builder SDK is closed")
        selected = self.policy.verification_commands if commands is None else tuple(commands)
        if any(command not in set(self.policy.verification_commands) for command in selected):
            raise ValueError("verification command is not operator-allowlisted")
        evidence = VerificationEvidence(started_at=time.time())
        if not selected:
            evidence.reason = "No verification commands configured."
            evidence.finished_at = time.time()
            result.evidence, result.status = evidence, "NOT_RUN"
            return evidence
        sha_result = await self.core.runner.run("git rev-parse --verify HEAD", timeout=20)
        sha = (getattr(sha_result, "stdout", "") or "").strip()
        if getattr(sha_result, "exit_code", 1) == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            evidence.target_sha = sha.lower()
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
        for command in selected:
            started = time.time()
            try:
                r = await self.core.runner.run(command, timeout=self.policy.verification_timeout_s)
                evidence.commands.append({
                    "command": command,
                    "exit_code": int(getattr(r, "exit_code", -1)),
                    "timed_out": bool(getattr(r, "timed_out", False)),
                    "stdout": str(getattr(r, "stdout", ""))[-12000:],
                    "stderr": str(getattr(r, "stderr", ""))[-12000:],
                    "duration_s": round(time.time() - started, 3),
                })
            except Exception as exc:
                evidence.commands.append({
                    "command": command, "exit_code": -1,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
        failed = [item for item in evidence.commands
                  if item.get("exit_code") != 0 or item.get("timed_out")]
        if failed:
            evidence.status = "FAIL"
            evidence.reason = f"{len(failed)} of {len(evidence.commands)} checks failed."
        elif self.policy.require_git_sha_for_verified and not evidence.target_sha:
            evidence.status = "PASS"
            evidence.reason = "Checks passed, but no exact Git SHA was found; result remains unverified."
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
