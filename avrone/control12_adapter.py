"""CONTROL12 v5 adapter for Avrone.

Emits typed control12.intent/v5 objects from Avrone tool calls so every
Avrone action is admissible only through CONTROL12's PLAN->POLICY->BUILD->
VERIFY->ADMIT->EXECUTE->OBSERVE->ATTEST->LEARN spine. No production action
is valid merely because a model emitted it — the adapter forces the intent
through schema validation and residual evidence attachment before dispatch.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import greatscale as gs

SCHEMA = "control12.intent/v5"
PLAN_SCHEMA = "control12.execution-plan/v5"

ENVIRONMENTS = {"development", "staging", "production"}


class Control12AdapterError(ValueError):
    pass


def build_intent(
    *,
    action: str,
    target_env: str,
    actor: str,
    rationale: str,
    constraints: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    """Construct a schema-valid CONTROL12 v5 intent with an attached residual."""
    if target_env not in ENVIRONMENTS:
        raise Control12AdapterError(f"invalid target environment: {target_env}")
    if not action or not isinstance(action, str):
        raise Control12AdapterError("action must be a non-empty string")
    if not actor or not isinstance(actor, str):
        raise Control12AdapterError("actor must be a non-empty string")

    intent: dict[str, Any] = {
        "schema": SCHEMA,
        "intent": intent_id or f"avrone-{uuid.uuid4().hex[:12]}",
        "action": action,
        "actor": actor,
        "rationale": rationale,
        "target": {"environment": target_env},
        "constraints": dict(constraints or {}),
        "created_at": time.time(),
    }

    # Attach residual evidence when the caller supplies measured check data.
    # Unmeasured intents carry null residual — they are admissible for
    # non-production targets only; production requires a measured channel.
    if evidence is not None:
        if "failures" in evidence and "runs" in evidence:
            intent["residual"] = gs.evidence_snapshot(
                int(evidence["failures"]), int(evidence["runs"]),
                float(evidence.get("alpha", 0.05)),
            )
        elif "h" in evidence:
            h = float(evidence["h"])
            s = gs.heat(h)
            intent["residual"] = {
                "h": h, "S": s, "logCoord": gs.log_coordinate(s),
                "psi": gs.psi(s), "coneOk": gs.cone_compliant(s),
                "orientation": "pass" if h == 0 else ("fail" if h == 1 else "measured"),
            }
        else:
            raise Control12AdapterError("evidence needs {failures, runs} or {h}")
    else:
        intent["residual"] = None

    return intent


def validate_intent(intent: dict[str, Any]) -> None:
    """Hard gate: schema, environment, and the production-residual rule."""
    if intent.get("schema") != SCHEMA:
        raise Control12AdapterError("unsupported intent schema")
    env = (intent.get("target") or {}).get("environment")
    if env not in ENVIRONMENTS:
        raise Control12AdapterError("invalid target environment")
    if env == "production" and intent.get("residual") is None:
        raise Control12AdapterError(
            "production intent requires measured residual evidence; "
            "a model token is not a measurement"
        )
    if intent.get("residual") is not None:
        r = intent["residual"]
        if "S" not in r or "psi" not in r:
            raise Control12AdapterError("residual missing S or psi")


def intent_to_plan(intent: dict[str, Any]) -> dict[str, Any]:
    """Mirror of CONTROL12 control-plane/planner.py build_execution_plan,
    extended with the residual field threaded through the DAG."""
    validate_intent(intent)
    steps = [
        {"id": "resolve-release", "action": "release.resolve", "requires": []},
        {"id": "verify-source", "action": "source.verify", "requires": ["resolve-release"]},
        {"id": "verify-sbom", "action": "sbom.verify", "requires": ["verify-source"]},
        {"id": "verify-signature", "action": "signature.verify", "requires": ["verify-source"]},
        {"id": "verify-provenance", "action": "provenance.verify", "requires": ["verify-source"]},
        {"id": "policy-admission", "action": "policy.evaluate",
         "requires": ["verify-sbom", "verify-signature", "verify-provenance"]},
        {"id": "deploy-canary", "action": "deployment.canary", "requires": ["policy-admission"]},
        {"id": "observe", "action": "telemetry.evaluate", "requires": ["deploy-canary"]},
        {"id": "promote", "action": "deployment.promote", "requires": ["observe"]},
        {"id": "attest", "action": "attestation.issue", "requires": ["promote"]},
        {"id": "ledger", "action": "ledger.append", "requires": ["attest"]},
    ]
    return {
        "schema": PLAN_SCHEMA,
        "intent": intent["intent"],
        "target": intent["target"],
        "constraints": intent.get("constraints", {}),
        "residual": intent.get("residual"),
        "steps": steps,
    }


def emit_typed_intent(ctx: Any, args: dict[str, Any]) -> str:
    """Avrone tool entry: build + validate + plan a CONTROL12 v5 intent."""
    try:
        intent = build_intent(
            action=str(args.get("action", "")),
            target_env=str(args.get("target_env", "development")),
            actor=str(args.get("actor", "avrone")),
            rationale=str(args.get("rationale", "")),
            constraints=args.get("constraints"),
            evidence=args.get("evidence"),
            intent_id=args.get("intent_id"),
        )
        plan = intent_to_plan(intent)
        if str(args.get("format", "text")).lower() != "json":
            lines = [
                f"control12 intent: {intent['intent']}  action={intent['action']}",
                f"target: {intent['target']['environment']}  actor: {intent['actor']}",
                f"residual: " + ("none (non-production)" if intent["residual"] is None
                                else f"S={intent['residual']['S']:.6g} "
                                     f"psi={intent['residual']['psi']:.6g} "
                                     f"orientation={intent['residual'].get('orientation')}"),
                f"plan steps: {len(plan['steps'])}  schema={plan['schema']}",
            ]
            return "\n".join(lines)
        return json.dumps({"intent": intent, "plan": plan}, sort_keys=True)
    except Control12AdapterError as e:
        return f"ERROR: {e}"
