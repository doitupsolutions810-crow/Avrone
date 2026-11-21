"""Resume continues from the checkpoint and does not rewrite the round inputs."""

import subprocess
import time
from pathlib import Path


def _commit(repo: Path, message: str) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "-m", message], cwd=repo, stdout=subprocess.DEVNULL)


def test_resume_keeps_completed_files_and_inputs(workspace, repo: Path):
    workspace.ingest(token="")
    change = workspace.create_change(
        title="two files",
        ref="main",
        required_files=["tests/test_ok.py", "tests/test_edge_slow.py"],
    )
    launched = workspace.launch_round(
        title="partial",
        ref="main",
        files=["tests/test_ok.py", "tests/test_edge_slow.py"],
        runner="python",
        change_id=change["change_id"],
        acceptance=True,
    )
    before = workspace.store.query_one("SELECT inputs_json, commit_sha FROM rounds WHERE id=?", (launched["id"],))
    stopped = workspace.execute_round(launched["id"], stop_after=1)
    assert stopped["status"] == "interrupted"
    assert stopped["files"][0]["status"] == "passed"
    assert stopped["files"][1]["status"] == "pending"
    assert stopped["checkpoints"]
    first_hash = stopped["files"][0]["log_sha256"]
    assert first_hash

    resumed = workspace.resume_round(launched["id"])
    assert resumed["status"] == "passed"
    assert resumed["inputs"]["files"] == ["tests/test_ok.py", "tests/test_edge_slow.py"]
    assert resumed["inputs"]["commit_sha"] == before["commit_sha"]
    after = workspace.store.query_one("SELECT inputs_json, commit_sha FROM rounds WHERE id=?", (launched["id"],))
    assert after["inputs_json"] == before["inputs_json"]
    assert after["commit_sha"] == before["commit_sha"]
    assert resumed["files"][0]["log_sha256"] == first_hash
    assert resumed["files"][1]["status"] == "passed"
    assert resumed["sealed"] == 1


def test_cancel_is_not_a_harness_timeout(workspace, repo: Path):
    target = repo / "tests" / "test_sleep.py"
    target.write_text("import time\n\ndef test_sleep():\n    time.sleep(30)\n", encoding="utf-8")
    _commit(repo, "sleep case")
    workspace.ingest(token="")
    launched = workspace.launch_round(
        title="cancel me",
        ref="main",
        files=["tests/test_sleep.py"],
        runner="python",
        background=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        detail = workspace.round_detail(launched["id"])
        if any(item["status"] == "running" for item in detail["files"]):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"round never started: {workspace.round_detail(launched['id'])['status']}")
    started = time.time()
    workspace.cancel_round(launched["id"])
    workspace._threads[launched["id"]].join(15)
    elapsed = time.time() - started
    detail = workspace.round_detail(launched["id"])
    assert detail["status"] == "cancelled"
    assert elapsed < 12
    assert any("no harness timeout" in item["message"] for item in detail["anomalies"])
