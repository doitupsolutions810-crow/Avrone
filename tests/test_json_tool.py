import asyncio
from pathlib import Path

from avrone.plugins import load_plugins
from avrone.tools import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent


def _reg():
    reg = ToolRegistry()
    reports = load_plugins(reg, None, directory=ROOT / "plugins", include_entry_points=False)
    by_name = {r["name"]: r for r in reports}
    assert by_name["json_tool"]["error"] is None
    assert by_name["json_tool"]["tools"] == ["json_tool"]
    assert reg.get("json_tool").source == "plugin:json_tool"
    return reg


def call(reg, **args):
    return asyncio.run(reg.call(None, "json_tool", args))


def test_json_validate_pretty_and_get():
    reg = _reg()
    raw = '{"ok": true, "items": [{"name": "ada"}, {"name": "linus"}], "n": 3}'
    assert call(reg, action="validate", text=raw) == "valid"
    bad = call(reg, action="validate", text='{"a": ')
    assert bad.startswith("ERROR: invalid JSON")
    pretty = call(reg, action="pretty", text=raw, indent=2)
    assert pretty.startswith("{\n") and '"ada"' in pretty
    assert call(reg, action="get", text=raw, path="items[0].name") == '"ada"'
    assert call(reg, action="get", text=raw, path="items.1.name") == '"linus"'
    assert call(reg, action="get", text=raw, path="n") == "3"
    assert call(reg, action="get", text=raw, path="ok") == "true"
    missing = call(reg, action="get", text=raw, path="items[9].name")
    assert missing.startswith("ERROR: path")
    assert call(reg, text="").startswith("ERROR")
    assert call(reg, action="nope", text=raw).startswith("ERROR")
