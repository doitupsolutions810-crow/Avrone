from __future__ import annotations

import hashlib

from avrone.artifact_verifier import verify_staged_artifacts


def task(limit=1024):
    return {"resource_limits": {"output_bytes": limit}}


def test_recomputes_artifact_digest_from_staged_bytes(tmp_path):
    data = b"independently verified output"
    (tmp_path / "result.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    result = {
        "artifact_digests": [digest],
        "artifacts": [{"path": "result.bin", "sha256": digest}],
    }
    assert verify_staged_artifacts(task(), result, root=tmp_path)


def test_rejects_worker_digest_lie(tmp_path):
    (tmp_path / "result.bin").write_bytes(b"actual bytes")
    result = {
        "artifact_digests": ["0" * 64],
        "artifacts": [{"path": "result.bin", "sha256": "0" * 64}],
    }
    assert not verify_staged_artifacts(task(), result, root=tmp_path)


def test_rejects_path_traversal(tmp_path):
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(b"not staged")
    digest = hashlib.sha256(b"not staged").hexdigest()
    result = {
        "artifact_digests": [digest],
        "artifacts": [{"path": "../outside.bin", "sha256": digest}],
    }
    assert not verify_staged_artifacts(task(), result, root=tmp_path)


def test_enforces_total_output_budget(tmp_path):
    data = b"x" * 20
    (tmp_path / "large.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    result = {
        "artifact_digests": [digest],
        "artifacts": [{"path": "large.bin", "sha256": digest}],
    }
    assert not verify_staged_artifacts(task(limit=10), result, root=tmp_path)


def test_rejects_missing_root_and_duplicate_digests(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_ARTIFACT_ROOT", raising=False)
    assert not verify_staged_artifacts(task(), {"artifact_digests": [], "artifacts": []})
    result = {"artifact_digests": ["a" * 64, "a" * 64], "artifacts": []}
    assert not verify_staged_artifacts(task(), result, root=tmp_path)


def test_empty_artifact_result_is_valid_when_explicitly_staged(tmp_path):
    assert verify_staged_artifacts(
        task(), {"artifact_digests": [], "artifacts": []}, root=tmp_path
    )


def test_rejects_unhashable_malformed_digest_values(tmp_path):
    result = {"artifact_digests": [{}], "artifacts": [{}]}
    assert not verify_staged_artifacts(task(), result, root=tmp_path)
