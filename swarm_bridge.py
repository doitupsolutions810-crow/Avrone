"""Swarm bridge: takes an ADMITTED intent from the harness and wraps it as a
swarm-vm-runner job with a signed capability envelope, dropping it into the
runner's queue directory. Reuses TrainLLM's envelope schema so the job's
lifecycle becomes another attested event in the attested-history index.

One writer, one reader. Zero new crypto, zero new gates.
The success criterion: a harness-emitted job appears in swarm-vm-runner's
ledger with a parent pointer back to the ADMITTED decision.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


QUEUE_DIR = Path("swarm-vm-runner/queue")
LEDGER_DIR = Path("swarm-vm-runner/ledger")


@dataclass
class CapabilityEnvelope:
    """Signed capability envelope — same schema TrainLLM's spine signs."""
    intent_id: str
    capability: str
    scope: str
    audience: str
    issued_at: float
    expires_at: float
    parent_decision: str  # ADMITTED decision id from the harness
    residual_s: float
    provenance_depth: int
    signature: str = ""

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def sign(self, key: str) -> "CapabilityEnvelope":
        self.signature = hashlib.sha256(
            (self.digest() + key).encode()
        ).hexdigest()
        return self


@dataclass
class SwarmJob:
    """A job the swarm-vm-runner fabric can execute."""
    job_id: str
    envelope: CapabilityEnvelope
    payload: dict[str, Any]
    queued_at: float = field(default_factory=time.time)
    status: str = "QUEUED"

    def to_ledger_entry(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "envelope_digest": self.envelope.digest(),
            "parent_decision": self.envelope.parent_decision,
            "capability": self.envelope.capability,
            "scope": self.envelope.scope,
            "residual_s": self.envelope.residual_s,
            "provenance_depth": self.envelope.provenance_depth,
            "status": self.status,
            "queued_at": self.queued_at,
            "signature": self.envelope.signature,
        }


def admit_to_swarm(
    admitted_intent: dict[str, Any],
    signing_key: str,
    queue_dir: Path = QUEUE_DIR,
) -> SwarmJob:
    """Bridge: ADMITTED intent -> signed swarm job -> queue directory.

    The parent_decision field is the load-bearing link: it points back to
    the harness's ADMITTED record, so the job's eventual ledger proof
    closes the chain from intent to execution to attestation.
    """
    envelope = CapabilityEnvelope(
        intent_id=admitted_intent["intent_id"],
        capability=admitted_intent["capability"],
        scope=admitted_intent.get("scope", "sandbox"),
        audience=admitted_intent.get("audience", "swarm-vm-runner"),
        issued_at=time.time(),
        expires_at=time.time() + admitted_intent.get("ttl", 3600),
        parent_decision=admitted_intent["decision_id"],
        residual_s=admitted_intent.get("residual_s", 0.0),
        provenance_depth=admitted_intent.get("provenance_depth", 0),
    ).sign(signing_key)

    job = SwarmJob(
        job_id=f"job-{envelope.digest()[:12]}",
        envelope=envelope,
        payload=admitted_intent.get("payload", {}),
    )

    queue_dir.mkdir(parents=True, exist_ok=True)
    job_path = queue_dir / f"{job.job_id}.json"
    job_path.write_text(json.dumps(asdict(job), indent=2, default=str))
    return job


def read_queued_jobs(queue_dir: Path = QUEUE_DIR) -> list[SwarmJob]:
    """Reader: scan the queue directory for pending jobs."""
    if not queue_dir.exists():
        return []
    jobs = []
    for path in sorted(queue_dir.glob("*.json")):
        data = json.loads(path.read_text())
        env = CapabilityEnvelope(**data["envelope"])
        jobs.append(SwarmJob(
            job_id=data["job_id"],
            envelope=env,
            payload=data["payload"],
            queued_at=data["queued_at"],
            status=data["status"],
        ))
    return jobs


def verify_job_against_index(
    job: SwarmJob, index_entries: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Success criterion: job's parent pointer must resolve to an ADMITTED
    entry in the attested-history index."""
    for entry in index_entries:
        if entry.get("id") == job.envelope.parent_decision:
            if entry.get("state") == "ADMITTED":
                return True, "parent ADMITTED found in index"
            return False, f"parent found but state is {entry.get('state')}"
    return False, "parent decision not found in index — unmeasured job"


if __name__ == "__main__":
    # Smoke test: admit -> queue -> read -> verify
    intent = {
        "intent_id": "int-001",
        "decision_id": "dec-001",
        "capability": "scale-sla",
        "scope": "sandbox",
        "residual_s": 0.0,
        "provenance_depth": 3,
        "payload": {"action": "echo", "args": ["hello"]},
    }
    job = admit_to_swarm(intent, signing_key="test-key")
    print("QUEUED:", job.job_id, job.envelope.digest()[:16])
    queued = read_queued_jobs()
    print("READ:", len(queued), "jobs")
    ok, reason = verify_job_against_index(
        job, [{"id": "dec-001", "state": "ADMITTED"}]
    )
    print("VERIFY:", ok, reason)
