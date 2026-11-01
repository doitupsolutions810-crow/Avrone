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
