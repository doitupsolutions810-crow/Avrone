from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List


@dataclass(frozen=True)
class ExecutionStep:
    id: str
    action: str
    requires: tuple[str, ...] = ()
    residual: Dict[str, Any] | None = None


def build_execution_plan(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Compile a validated CONTROL12 intent into a deterministic execution DAG.

    Emergent-compile extension: the residual field from the intent is
    threaded through every step so the ledger can attest measured
    evidence, not model tokens. Production intents without residual are
    rejected upstream by avrone.control12_adapter before they reach here.
    """
    if intent.get("schema") != "control12.intent/v5":
        raise ValueError("unsupported intent schema")

    target = intent.get("target") or {}
    constraints = intent.get("constraints") or {}
    environment = target.get("environment")
    if environment not in {"development", "staging", "production"}:
        raise ValueError("invalid target environment")

    residual = intent.get("residual")

    steps: List[ExecutionStep] = [
        ExecutionStep("resolve-release", "release.resolve"),
        ExecutionStep("verify-source", "source.verify", ("resolve-release",)),
        ExecutionStep("verify-sbom", "sbom.verify", ("verify-source",)),
        ExecutionStep("verify-signature", "signature.verify", ("verify-source",)),
        ExecutionStep("verify-provenance", "provenance.verify", ("verify-source",)),
        ExecutionStep("policy-admission", "policy.evaluate", (
            "verify-sbom", "verify-signature", "verify-provenance"
        ), residual),
        ExecutionStep("deploy-canary", "deployment.canary", ("policy-admission",), residual),
        ExecutionStep("observe", "telemetry.evaluate", ("deploy-canary",), residual),
        ExecutionStep("promote", "deployment.promote", ("observe",), residual),
        ExecutionStep("attest", "attestation.issue", ("promote",), residual),
        ExecutionStep("ledger", "ledger.append", ("attest",), residual),
    ]

    return {
        "schema": "control12.execution-plan/v5",
        "intent": intent["intent"],
        "target": target,
        "constraints": constraints,
        "residual": residual,
        "steps": [asdict(step) for step in steps],
    }
