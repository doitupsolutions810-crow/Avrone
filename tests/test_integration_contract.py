import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_integration_contract_has_pinned_revisions_and_holds_all_edges():
    contracts = json.loads((ROOT / "integration" / "contracts.json").read_text())
    assert contracts["schema"] == "avrone.integration-contracts/v1"
    assert contracts["promotion_default"] == "HOLD"
    assert len(contracts["repositories"]) == 7
    for repo in contracts["repositories"]:
        assert re.fullmatch(r"[0-9a-f]{40}", repo["revision"]), repo
    assert all(edge["state"] == "UNVERIFIED" for edge in contracts["edges"])
    assert len(contracts["edges"]) == 6


def test_promotion_policy_has_independent_evidence_gates():
    policy = json.loads((ROOT / "integration" / "promotion-policy.json").read_text())
    assert policy["default_decision"] == "HOLD"
    required = policy["required_checks"]
    for key in (
        "pinned_revision",
        "schema_compatibility",
        "independent_identity_verification",
        "exact_action_resource_method_authorization",
        "durable_signed_receipt",
        "idempotency_and_replay_tests",
        "failure_recovery_tests",
        "independent_evaluator",
        "complete_residual_measurement",
    ):
        assert required[key] is True
    assert "approve_own_change" in policy["learning"]["forbidden"]
