"""Shared fixtures. Repositories created here are temporary and local."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> None:
    subprocess.check_call(["git", *args], cwd=repo, stdout=subprocess.DEVNULL)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "crow@example.com")
    git(root, "config", "user.name", "Crow")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "test_edge_slow.py").write_text(
        "def test_edge_slow():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_edge_broken.py").write_text("def test_bad(:\n", encoding="utf-8")
    (root / "evidence").mkdir()
    (root / "evidence" / "sample-evidence.json").write_text('{"source": "fixture"}\n', encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture()
def workspace(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    from crow_os.db import Store
    from crow_os.service import Workspace

    store = Store(tmp_path / "var" / "crow.sqlite")
    return Workspace(store, repo, tmp_path / "worktrees")
