"""Offline edge tests for avrone.plugins: failure reporting, sourcing, entry points."""
import logging
import textwrap

import pytest

from avrone import plugins as pl
from avrone.tools import Tool, ToolRegistry


def _reg():
    return ToolRegistry()


def _write(d, name, body):
    p = d / name
    p.write_text(textwrap.dedent(body))
    return p


def test_register_exception_is_reported_but_partial_tools_kept(tmp_path, caplog):
    _write(tmp_path, "half.py", """
        from avrone.tools import Tool
        def register(registry, settings):
            registry.register(Tool("half_ok", "first", "{}", lambda: "x"))
            raise ValueError("second tool exploded")
    """)
    reg = _reg()
    with caplog.at_level(logging.WARNING, logger="avrone.plugins"):
        reports = pl.load_plugins(reg, directory=tmp_path, include_entry_points=False)
    assert reports == [{"name": "half", "source": str(tmp_path / "half.py"),
                        "tools": ["half_ok"], "error": "ValueError: second tool exploded"}]
    assert reg.get("half_ok").source == "plugin:half"
    assert any("register() failed" in r.getMessage() for r in caplog.records)


def test_explicit_tool_source_is_preserved(tmp_path):
    _write(tmp_path, "tagged.py", """
        from avrone.tools import Tool
        def register(registry, settings):
            t = Tool("tagged_t", "d", "{}", lambda: "x")
            t.source = "custom:mine"
            registry.register(t)
    """)
    reg = _reg()
    pl.load_plugins(reg, directory=tmp_path, include_entry_points=False)
    assert reg.get("tagged_t").source == "custom:mine"


def test_missing_register_and_syntax_error_reported(tmp_path):
    _write(tmp_path, "noreg.py", "X = 1\n")
    _write(tmp_path, "broken.py", "def (:\n")
    _write(tmp_path, "_private.py", "raise SystemExit('must never import')\n")
    _write(tmp_path, ".hidden.py", "raise SystemExit('must never import')\n")
    _write(tmp_path, "notes.txt", "ignored")
    (tmp_path / "emptydir").mkdir()  # no __init__.py -> not a plugin
    reports = {r["name"]: r for r in pl.load_plugins(_reg(), directory=tmp_path, include_entry_points=False)}
    assert set(reports) == {"noreg", "broken"}
    assert reports["noreg"]["error"].startswith("AttributeError: plugin has no register")
    assert reports["broken"]["error"].startswith("SyntaxError")
    assert "avrone_plugin_broken" not in __import__("sys").modules  # failed import is not cached


def test_package_plugin_with_relative_import(tmp_path):
    pkg = tmp_path / "pkgplug"
    pkg.mkdir()
    _write(pkg, "helper.py", "VALUE = 'from-helper'\n")
    _write(pkg, "__init__.py", """
        from avrone.tools import Tool
        from .helper import VALUE
        def register(registry, settings):
            registry.register(Tool("pkg_t", VALUE, "{}", lambda: VALUE))
    """)
    reg = _reg()
    reports = pl.load_plugins(reg, settings={"k": 1}, directory=tmp_path, include_entry_points=False)
    assert reports[0]["error"] is None and reports[0]["tools"] == ["pkg_t"]
    assert reg.get("pkg_t").source == "plugin:pkgplug"


def test_load_file_raises_when_no_spec(monkeypatch, tmp_path):
    monkeypatch.setattr(pl.importlib.util, "spec_from_file_location", lambda *a, **k: None)
    with pytest.raises(ImportError, match="cannot load"):
        pl._load_file(tmp_path / "x.py")


def test_plugins_dir_env_and_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(tmp_path / "nope"))
    assert pl.plugins_dir() == (tmp_path / "nope").resolve()
    assert pl.load_plugins(_reg(), include_entry_points=False) == []


# ------------------------------------------------------------ entry points
class FakeEP:
    def __init__(self, name, value, obj=None, exc=None):
        self.name, self.value, self._obj, self._exc = name, value, obj, exc

    def load(self):
        if self._exc:
            raise self._exc
        return self._obj


def _func_register(registry, settings):
    registry.register(Tool("ep_func_t", "d", "{}", lambda: "x"))


class _ModLike:
    @staticmethod
    def register(registry, settings):
        registry.register(Tool("ep_mod_t", "d", "{}", lambda: settings))


def test_entry_points_function_module_and_failures(monkeypatch, tmp_path):
    eps = [
        FakeEP("func", "pkg:register", obj=_func_register),
        FakeEP("mod", "pkg2", obj=_ModLike),
        FakeEP("dud", "pkg3", obj=42),
        FakeEP("crash", "pkg4", exc=ModuleNotFoundError("no pkg4")),
    ]
    seen = {}

    def fake_entry_points(group):
        seen["group"] = group
        return eps

    monkeypatch.setattr(pl, "entry_points", fake_entry_points)
    reg = _reg()
    reports = {r["name"]: r for r in pl.load_plugins(reg, directory=tmp_path / "none")}
    assert seen["group"] == "avrone.plugins"
    assert reports["func"] == {"name": "func", "source": "entrypoint:pkg:register",
                               "tools": ["ep_func_t"], "error": None}
    assert reports["mod"]["tools"] == ["ep_mod_t"] and reports["mod"]["error"] is None
    assert reg.get("ep_func_t").source == "plugin:func"
    assert reg.get("ep_mod_t").source == "plugin:mod"
    assert reports["dud"]["error"] == "AttributeError: entry point has no register(registry, settings)"
    assert reports["dud"]["tools"] == []
    assert reports["crash"]["error"] == "ModuleNotFoundError: no pkg4"
