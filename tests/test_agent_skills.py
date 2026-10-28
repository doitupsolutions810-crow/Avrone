"""Check that the code-review skill is discoverable and loadable by agents."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from avrone.agentkit import t_skill_list, t_skill_load


ROOT = Path(__file__).resolve().parents[1]


def ctx(tmp_path):
    core = SimpleNamespace(
        runner=SimpleNamespace(workspace=tmp_path),
        settings=SimpleNamespace(data_dir=tmp_path / "data"),
        codex=SimpleNamespace(emit=lambda *a, **k: None),
    )
    return SimpleNamespace(core=core, run_id="skills-test", name="Test-Agent", depth=0)


def test_code_review_skill_is_valid_and_loadable(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(ROOT / "skills"))
    path = ROOT / "skills" / "code-review" / "SKILL.md"
    assert path.is_file()
    text = path.read_text()
    assert text.startswith("---\nname: code-review\n")
    assert "Never describe a proposed or unexecuted test as passing" in text
    listing = asyncio.run(t_skill_list(ctx(tmp_path), {}))
    assert "code-review:" in listing
    loaded = asyncio.run(t_skill_load(ctx(tmp_path), {"name": "code-review"}))
    assert loaded.startswith("---\nname: code-review\n")
    assert "AVRONE_RECON_ALLOWLIST" in loaded


def test_release_verification_skill_is_valid_and_loadable(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(ROOT / "skills"))
    path = ROOT / "skills" / "release-verification" / "SKILL.md"
    assert path.is_file()
    text = path.read_text()
    assert text.startswith("---\nname: release-verification\n")
    assert "missing runner" in text
    listing = asyncio.run(t_skill_list(ctx(tmp_path), {}))
    assert "release-verification:" in listing
    loaded = asyncio.run(t_skill_load(ctx(tmp_path), {"name": "release-verification"}))
    assert "avrone.zip" in loaded
    assert "unavailable" in loaded
