import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "integration" / "schemas"


def test_message_envelope_schema_matches_contract_required_fields():
    contracts = json.loads((ROOT / "integration" / "contracts.json").read_text())
    schema = json.loads((SCHEMAS / "message-envelope-v1.schema.json").read_text())
    assert schema["properties"]["schema"]["const"] == "avrone.message-envelope/v1"
    assert set(schema["required"]) == set(contracts["required_envelope"])
    assert schema["additionalProperties"] is False


def test_authorization_receipt_schema_matches_contract_required_fields():
    contracts = json.loads((ROOT / "integration" / "contracts.json").read_text())
    schema = json.loads((SCHEMAS / "authorization-receipt-v1.schema.json").read_text())
    assert set(schema["required"]) == set(contracts["required_receipt"])
    assert schema["additionalProperties"] is False
    assert "authorized_not_executed" in schema["properties"]["outcome"]["enum"]
    assert "succeeded" in schema["properties"]["outcome"]["enum"]
