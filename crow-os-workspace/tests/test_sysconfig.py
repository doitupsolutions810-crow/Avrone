"""Secret masking and the system-configuration snapshot."""

import json
import subprocess
from pathlib import Path

from crow_os.connectors import github_https_url
from crow_os.redact import mask_text, mask_value
from crow_os.sysconfig import capture_system


def test_masks_tokens_urls_and_env(tmp_path: Path, monkeypatch):
    assert mask_text("https://x-access-token:super-secret-token@github.com/acme/widgets.git") == "https://github.com/acme/widgets.git"
    assert "super-secret-token" not in mask_text("https://user:super-secret-token@github.com/acme/widgets")
    assert mask_value("GITHUB_TOKEN", "ghp_supersecretvalue") == "••••"
    assert mask_value("PATH", "/usr/bin") == "/usr/bin"

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "crow@example.com"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "Crow"], cwd=repo)
    subprocess.check_call(
        ["git", "remote", "add", "origin", "https://x-access-token:super-secret-token@github.com/acme/widgets.git"],
        cwd=repo,
    )
    (repo / ".env").write_text("AVRONE_ADMIN_TOKEN=plain-secret-value\nPLAIN=visible\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue")
    monkeypatch.setenv("PATH", "/usr/bin")
    snapshot = capture_system(repo)
    blob = json.dumps(snapshot)
    assert "super-secret-token" not in blob
    assert "ghp_supersecretvalue" not in blob
    assert "plain-secret-value" not in blob
    assert snapshot["env"]["GITHUB_TOKEN"] == "••••"
    assert snapshot["env"]["PATH"] == "/usr/bin"
    assert "visible" in blob
    assert snapshot["toolchain"]["git"].startswith("git version")
    assert github_https_url("https://x-access-token:super-secret-token@github.com/acme/widgets.git") == "https://github.com/acme/widgets"


def test_round_record_does_not_copy_tokens(workspace, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue")
    monkeypatch.setenv("XAI_API_KEY", "xai-supersecretvalue")
    workspace.ingest(token="")
    launched = workspace.launch_round(
        title="env",
        ref="main",
        files=["tests/test_ok.py"],
        runner="python",
        acceptance=False,
    )
    blob = json.dumps(launched["env"])
    assert "ghp_supersecretvalue" not in blob
    assert "xai-supersecretvalue" not in blob
    assert launched["env"]["github_token_present"] is True
    config = workspace.system_config(refresh=True)
    assert "ghp_supersecretvalue" not in json.dumps(config)
    assert "xai-supersecretvalue" not in json.dumps(config)
