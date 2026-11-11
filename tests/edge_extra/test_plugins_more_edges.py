"""Round 4 plugin loader edges: symlinks, broken links, binary files, register-less modules."""
from __future__ import annotations

from avrone.plugins import load_plugins
from avrone.tools import ToolRegistry


PLUGIN = '''
from avrone.tools import Tool

def register(registry, settings):
    async def fn(ctx, args):
        return "ok"
    registry.register(Tool("symlink_tool", "from a symlinked plugin", "{}", fn, source="plugin:linked"))
'''


def test_load_plugins_follows_symlink_py(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "linked.py").write_text(PLUGIN)
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "linked.py").symlink_to(real_dir / "linked.py")
    reports = load_plugins(ToolRegistry(), settings=None, directory=plug, include_entry_points=False)
    by = {r["name"]: r for r in reports}
    assert "linked" in by
    assert by["linked"]["error"] is None
    assert "symlink_tool" in by["linked"]["tools"]


def test_load_plugins_skips_broken_symlink_and_reports_binary(tmp_path):
    plug = tmp_path / "plugins"
    plug.mkdir()
    (plug / "gone.py").symlink_to(tmp_path / "missing.py")
    (plug / "binary.py").write_bytes(b"\x00\xff\xfe this is not python\x00")
    reports = load_plugins(ToolRegistry(), settings=None, directory=plug, include_entry_points=False)
    names = {r["name"] for r in reports}
    # A broken symlink is not a file, so it is skipped rather than crashing.
    assert "gone" not in names
    assert "binary" in names
    assert reports[0]["error"] or any(r["error"] for r in reports if r["name"] == "binary")


def test_load_plugins_directory_is_itself_a_symlink(tmp_path):
    real = tmp_path / "real-plugins"
    real.mkdir()
    (real / "linked.py").write_text(PLUGIN)
    link = tmp_path / "plugins-link"
    link.symlink_to(real)
    reports = load_plugins(ToolRegistry(), settings=None, directory=link, include_entry_points=False)
    assert any(r["name"] == "linked" and r["error"] is None for r in reports)
