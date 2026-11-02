from __future__ import annotations

import hashlib
import json

import pytest

from avrone.research_bridge import (
    ResearchBridgeError,
    read_receipt,
    validate_source,
    fetch_source_files,
    write_receipt,
)


def test_validate_source_requires_full_sha():
    repo, sha = validate_source("owner/repo", "A" * 40)
    assert repo == "owner/repo"
    assert sha == "a" * 40
    with pytest.raises(ResearchBridgeError):
        validate_source("owner/repo", "deadbeef")
    with pytest.raises(ResearchBridgeError):
        validate_source("https://example.com/owner/repo", "a" * 40)


def test_receipt_binds_source_run_and_result_digest(tmp_path):
    answer = "counterexample found"
    result = {"run_id": "run-123", "answer": answer, "agents": ["algebraist", "skeptic"]}
    receipt = write_receipt(
        tmp_path,
        run_id="run-123",
        source={"repository": "owner/repo", "commit_sha": "a" * 40,
                "commit_url": "https://github.com/owner/repo/commit/" + "a" * 40,
                "verification": "github_public_commit_api_exact_sha"},
        claim="R and P commute on the tested domain",
        mode="deep",
        rounds=2,
        result=result,
    )
    assert receipt["result_digest_sha256"] == hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    stored = read_receipt(tmp_path, "run-123")
    assert stored["source"]["commit_sha"] == "a" * 40
    assert stored["run_id"] == "run-123"
    assert stored["result_digest_sha256"] == receipt["result_digest_sha256"]
    binding = {key: stored[key] for key in ("schema", "run_id", "source", "claim", "mode", "rounds_requested", "result_digest_sha256")}
    expected_evidence_digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    assert stored["evidence_digest_sha256"] == expected_evidence_digest
    assert json.loads((tmp_path / "research_receipts" / "run-123.json").read_text())["schema"] == "avrone.research-receipt.v1"


def test_receipt_path_is_not_traversable(tmp_path):
    with pytest.raises(ResearchBridgeError):
        read_receipt(tmp_path, "../secret")


@pytest.mark.asyncio
async def test_source_ingestion_rejects_path_traversal_before_network():
    with pytest.raises(ResearchBridgeError):
        await fetch_source_files("owner/repo", "a" * 40, ["../secrets.txt"])


@pytest.mark.asyncio
async def test_source_ingestion_caps_requested_file_count():
    with pytest.raises(ResearchBridgeError):
        await fetch_source_files("owner/repo", "a" * 40, ["f" + str(i) for i in range(6)])


def _write_valid_test_receipt(tmp_path, run_id="secure-run"):
    result = {"answer": "verified", "agents": ["skeptic"]}
    receipt = write_receipt(
        tmp_path, run_id=run_id,
        source={"repository": "owner/repo", "commit_sha": "a" * 40,
                "verification": "github_commit_api_exact_sha"},
        claim="test claim", mode="deep", rounds=2, result=result,
    )
    return tmp_path / "research_receipts" / f"{run_id}.json", receipt


def test_receipt_rejects_modified_result(tmp_path):
    path, receipt = _write_valid_test_receipt(tmp_path)
    stored = json.loads(path.read_text())
    stored["result"]["answer"] = "tampered"
    path.write_text(json.dumps(stored))
    with pytest.raises(ResearchBridgeError, match="result digest"):
        read_receipt(tmp_path, "secure-run")


def test_receipt_rejects_modified_evidence_binding(tmp_path):
    path, receipt = _write_valid_test_receipt(tmp_path)
    stored = json.loads(path.read_text())
    stored["claim"] = "different claim"
    path.write_text(json.dumps(stored))
    with pytest.raises(ResearchBridgeError, match="evidence digest"):
        read_receipt(tmp_path, "secure-run")


def test_receipt_rejects_symlink(tmp_path):
    path, receipt = _write_valid_test_receipt(tmp_path)
    real = tmp_path / "outside.json"
    real.write_text(path.read_text())
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(ResearchBridgeError):
        read_receipt(tmp_path, "secure-run")


def test_receipt_rejects_oversized_file(tmp_path):
    directory = tmp_path / "research_receipts"
    directory.mkdir()
    (directory / "huge.json").write_bytes(b" " * 2_100_001)
    with pytest.raises(ResearchBridgeError, match="size limit"):
        read_receipt(tmp_path, "huge")


def test_receipt_rejects_non_finite_json(tmp_path):
    directory = tmp_path / "research_receipts"
    directory.mkdir()
    (directory / "nan.json").write_text('{"schema":"avrone.research-receipt.v1","value":NaN}')
    with pytest.raises(ResearchBridgeError):
        read_receipt(tmp_path, "nan")
