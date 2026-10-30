"""Production wiring for the AVRONE authority -> runner -> verifier path.

The runner is deliberately injected: this module does not pretend that a local
process or persistent container is a disposable VM. Production callers must
supply a runner adapter that provisions, bounds, and destroys an isolated job VM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .artifact_verifier import verify_staged_artifacts_async
from .capability_authority import authorize_with_capability_authority
from .vertical_slice import execute_vertical_slice

Runner = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def execute_with_capability_authority(
    envelope: Mapping[str, Any],
    *,
    allowed_capabilities: set[str] | frozenset[str],
    run_isolated: Runner,
    authority_url: str | None = None,
    artifact_root: str | Path | None = None,
    policy_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Run AVRONE's real signed-authority client and independent byte verifier.

    Authorization fails closed in the authority adapter. Artifact bytes are
    reopened and SHA-256 recomputed by AVRONE after the runner returns.
    """
    async def authorize(task: Mapping[str, Any]) -> Mapping[str, Any]:
        return await authorize_with_capability_authority(
            task, base_url=authority_url, timeout_seconds=policy_timeout_seconds
        )

    async def verify(task: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
        return await verify_staged_artifacts_async(task, result, root=artifact_root)

    return await execute_vertical_slice(
        envelope,
        allowed_capabilities=allowed_capabilities,
        authorize=authorize,
        run_isolated=run_isolated,
        verify_artifacts=verify,
        policy_timeout_seconds=policy_timeout_seconds,
    )
