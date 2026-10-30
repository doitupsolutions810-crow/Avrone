from avrone.bootstrap import apply_tool_focus
from avrone.tools import builtin_registry


def test_focus_keeps_only_listed():
    r = builtin_registry()
    removed = apply_tool_focus(r, "read_file, write_file,nope")
    assert set(r.names()) == {"read_file", "write_file"}
    assert "shell" in removed


def test_focus_empty_keeps_all():
    r = builtin_registry()
    before = r.names()
    assert apply_tool_focus(r, "") == []
    assert r.names() == before


def test_focus_env(monkeypatch):
    monkeypatch.setenv("AVRONE_TOOLS_ONLY", "shell")
    r = builtin_registry()
    apply_tool_focus(r)
    assert r.names() == ["shell"]
