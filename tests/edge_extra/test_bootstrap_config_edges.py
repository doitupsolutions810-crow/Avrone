"""Round 4 config / bootstrap edges: env bool whitespace, symlink paths, junk Settings, tool focus."""
from __future__ import annotations

import os
from pathlib import Path

from avrone.bootstrap import apply_tool_focus
from avrone.config import Settings, _bool, parse_dotenv_line
from avrone.tools import builtin_registry


def test_bool_env_strips_whitespace(monkeypatch):
    read = _bool("AVRONE_GOVERNOR", True)
    monkeypatch.setenv("AVRONE_GOVERNOR", " true ")
    assert read() is True
    monkeypatch.setenv("AVRONE_GOVERNOR", "\tOFF\n")
    assert read() is False
    monkeypatch.setenv("AVRONE_GOVERNOR", " 1 ")
    assert read() is True
    monkeypatch.delenv("AVRONE_GOVERNOR")
    assert read() is True  # default


def test_settings_workspace_follows_symlink(tmp_path, monkeypatch):
    real = tmp_path / "real-ws"
    real.mkdir()
    link = tmp_path / "link-ws"
    link.symlink_to(real)
    monkeypatch.setenv("AVRONE_WORKSPACE", str(link))
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    s = Settings()
    assert s.workspace == real.resolve()
    assert s.workspace.is_dir()


def test_settings_whitespace_and_empty_path_do_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_WORKSPACE", "   ")
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "data"))
    s = Settings()
    assert isinstance(s.workspace, Path)


def test_dotenv_export_and_blank_and_unknown_keys(tmp_path):
    assert parse_dotenv_line("") is None
    assert parse_dotenv_line("   # comment") is None
    assert parse_dotenv_line("export AVRONE_LLM_MODE=mock") == ("AVRONE_LLM_MODE", "mock")
    assert parse_dotenv_line("NOT_A_KEY") is None
    assert parse_dotenv_line("SPACED KEY=v") is None


def test_apply_tool_focus_whitespace_only_and_duplicates():
    r = builtin_registry()
    before = r.names()
    assert apply_tool_focus(r, "  ,  ,\t") == []
    assert r.names() == before
    removed = apply_tool_focus(r, "shell, shell,  shell  ")
    assert r.names() == ["shell"]
    assert "shell" not in removed
