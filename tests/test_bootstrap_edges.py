"""Edge cases for avrone.bootstrap: hook calling conventions, optional extensions on/off,
hook failures, cleanup ordering. Offline; the runner is faked so nothing is spawned."""
from __future__ import annotations

import sys

import pytest

import avrone.bootstrap as bs
from avrone.config import Settings
from avrone.tools import Tool, ToolRegistry, builtin_registry

ENV = {"registry": "REG", "settings": "SET", "core": "CORE"}


# ------------------------------------------------------------------ _flex_args / _call

def test_flex_args_named_aliases():
    def f(registry, settings, core): ...
    assert bs._flex_args(f, ENV) == ([], {"registry": "REG", "settings": "SET", "core": "CORE"})

    def g(tools, cfg, trinity=None): ...
    assert bs._flex_args(g, ENV) == ([], {"tools": "REG", "cfg": "SET", "trinity": "CORE"})


def test_flex_args_positional_only_alias():
    def f(reg, s, /): ...
    assert bs._flex_args(f, ENV) == (["REG", "SET"], {})


def test_flex_args_unnamed_required_in_order():
    def f(a, b, c, d): ...
    # registry, settings, core, then None once the order runs out
    assert bs._flex_args(f, ENV) == (["REG", "SET", "CORE", None], {})


def test_flex_args_unnamed_keyword_only_and_defaults_skipped():
    def f(a, *, b, c="keep"): ...
    assert bs._flex_args(f, ENV) == (["REG"], {"b": "SET"})


def test_flex_args_varargs_ignored():
    def f(*args, **kwargs): ...
    assert bs._flex_args(f, ENV) == ([], {})


def test_flex_args_mixed_alias_and_unnamed():
    def f(x, core, y): ...
    # aliases bind by name; unnamed params take registry, settings in order
    assert bs._flex_args(f, ENV) == (["REG", "SET"], {"core": "CORE"})


def test_flex_args_unsignaturable_falls_back_to_registry(monkeypatch):
    def boom(_fn):
        raise ValueError("no signature")
    monkeypatch.setattr(bs.inspect, "signature", boom)
    assert bs._flex_args(print, ENV) == (["REG"], {})


@pytest.mark.asyncio
async def test_call_sync_and_async():
    def sync(registry):
        return ("sync", registry)

    async def coro(settings, core):
        return ("async", settings, core)

    assert await bs._call(sync, ENV) == ("sync", "REG")
    assert await bs._call(coro, ENV) == ("async", "SET", "CORE")


@pytest.mark.asyncio
async def test_call_class_constructor():
    class Mgr:
        def __init__(self, config, registry=None):
            self.cfg, self.reg = config, registry
    m = await bs._call(Mgr, ENV)
    assert (m.cfg, m.reg) == ("SET", "REG")


# ------------------------------------------------------------------ _describe / _sources

def test_describe_scalars_and_containers():
    assert bs._describe(None) is None
    assert bs._describe(3) == 3 and bs._describe("x") == "x" and bs._describe(True) is True
    assert bs._describe({1: (2, {3})}) == {"1": [2, [3]]}


def test_describe_status_to_dict_as_dict_and_fallbacks():
    class S:
        def status(self):
            return {"ok": True}

    class BadStatus:
        def status(self):
            raise RuntimeError("x")

        def to_dict(self):
            return [1]

    class AsDict:
        def as_dict(self):
            return {"a": 1}

    class Named:
        name = "srv"

    class Plain:
        pass

    assert bs._describe(S()) == {"ok": True}
    assert bs._describe(BadStatus()) == [1]  # status() failing falls through to to_dict()
    assert bs._describe(AsDict()) == {"a": 1}
    assert bs._describe(Named()) == "srv"
    r = bs._describe(Plain())
    assert isinstance(r, str) and "Plain" in r and len(r) <= 200


def test_describe_truncates_repr():
    class Long:
        def __repr__(self):
            return "x" * 1000
    assert len(bs._describe(Long())) == 200


def test_sources_counts():
    reg = builtin_registry()
    counts = bs._sources(reg)
    assert counts and sum(counts.values()) == len(reg.list())
    assert bs._sources(ToolRegistry()) == {}


# ------------------------------------------------------------------ build_core

class FakeRunner:
    kind = "fake"

    def __init__(self, log):
        self.log = log

    async def stop(self):
        self.log.append("runner.stop")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.delenv("AVRONE_FLEET_PARENT", raising=False)
    monkeypatch.setenv("AVRONE_MCP_CONFIG", str(tmp_path / "no-mcp.json"))
    monkeypatch.setenv("AVRONE_PLUGINS_DIR", str(tmp_path / "no-plugins"))
    return Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data" / "nested", agents_can_shell=False,
                    allow_network=False)


@pytest.fixture
def log(monkeypatch):
    calls: list[str] = []

    async def fake_establish(_s):
        calls.append("establish")
        return FakeRunner(calls)
    monkeypatch.setattr(bs, "establish", fake_establish)
    return calls


@pytest.mark.asyncio
async def test_extras_off_skips_hooks(settings, log, monkeypatch):
    import avrone.tools_extra as te

    def never(*a, **k):
        raise AssertionError("hook must not run with extras=False")
    monkeypatch.setattr(te, "register_extra_tools", never)
    core, cleanups = await bs.build_core(settings, extras=False)
    assert settings.data_dir.is_dir()  # created before the runner
    ext = core.extensions
    assert ext["extra_tools"] is None and ext["plugins"] is None and ext["mcp"] is None
    assert ext["errors"] == [] and ext["tool_sources"]
    assert "fleet" not in ext
    assert len(cleanups) == 1
    await bs.run_cleanups(cleanups)
    assert log == ["establish", "runner.stop"]


@pytest.mark.asyncio
async def test_extras_on_real_hooks_no_errors(settings, log):
    core, cleanups = await bs.build_core(settings, extras=True)
    ext = core.extensions
    assert ext["errors"] == [], ext["errors"]
    assert ext["extra_tools"]["installed"] is True and ext["extra_tools"]["added"]
    assert ext["plugins"]["installed"] is True
    assert ext["mcp"]["installed"] is True
    assert "error" not in ext["fleet"]  # no AVRONE_FLEET_TOKEN -> fleet present but off
    assert sum(ext["tool_sources"].values()) == len(core.tools.list())
    await bs.run_cleanups(cleanups)
    assert log[-1] == "runner.stop"


def _block_imports(monkeypatch, *mods):
    for m in mods:
        monkeypatch.setitem(sys.modules, m, None)  # `from x import y` -> ImportError


@pytest.mark.asyncio
async def test_extensions_not_installed(settings, log, monkeypatch):
    _block_imports(monkeypatch, "avrone.tools_extra", "avrone.plugins", "avrone.mcp_client")
    core, cleanups = await bs.build_core(settings)
    ext = core.extensions
    assert ext["extra_tools"] == {"installed": False}
    assert ext["plugins"] == {"installed": False}
    assert ext["mcp"] == {"installed": False}
    assert ext["errors"] == []
    await bs.run_cleanups(cleanups)


@pytest.mark.asyncio
async def test_hook_failures_are_isolated(settings, log, monkeypatch):
    import avrone.mcp_client as mc
    import avrone.plugins as pl
    import avrone.tools_extra as te

    def bad_tools(registry):
        raise RuntimeError("tools boom")

    async def bad_plugins(registry, settings):
        raise ValueError("plugins boom")

    class BadMgr:
        def __init__(self):
            raise OSError("mcp boom")
    monkeypatch.setattr(te, "register_extra_tools", bad_tools)
    monkeypatch.setattr(pl, "load_plugins", bad_plugins)
    monkeypatch.setattr(mc, "MCPManager", BadMgr)
    core, cleanups = await bs.build_core(settings)
    ext = core.extensions
    assert ext["extra_tools"] == {"installed": True, "error": "RuntimeError: tools boom"}
    assert ext["plugins"] == {"installed": True, "error": "ValueError: plugins boom"}
    assert ext["mcp"] == {"installed": True, "error": "OSError: mcp boom"}
    assert ext["errors"] == ["tools_extra: RuntimeError: tools boom", "plugins: ValueError: plugins boom",
                             "mcp: OSError: mcp boom"]
    assert core.tools.list()  # core still usable
    await bs.run_cleanups(cleanups)


@pytest.mark.asyncio
async def test_hooks_report_added_tools_and_results(settings, log, monkeypatch):
    import avrone.mcp_client as mc
    import avrone.plugins as pl
    import avrone.tools_extra as te

    async def _noop(ctx, args):
        return "ok"

    def add(registry, name, source):
        registry.register(Tool(name, f"{name} tool", "{}", _noop, source=source))

    def tools_hook(tools):
        add(tools, "zz_extra", "extra")
        return {"count": 1}

    def plugins_hook(reg, cfg):
        add(reg, "zz_plugin", "plugin")
        return ["p1"]

    monkeypatch.setattr(te, "register_extra_tools", tools_hook)
    monkeypatch.setattr(pl, "load_plugins", plugins_hook)
    monkeypatch.setattr(mc, "MCPManager", lambda: None)  # None manager: no methods to call
    core, cleanups = await bs.build_core(settings)
    ext = core.extensions
    assert ext["extra_tools"]["added"] == ["zz_extra"] and ext["extra_tools"]["result"] == {"count": 1}
    assert ext["plugins"]["added"] == ["zz_plugin"] and ext["plugins"]["loaded"] == ["p1"]
    assert ext["mcp"]["installed"] is True and ext["mcp"]["added"] == []
    await bs.run_cleanups(cleanups)


@pytest.mark.asyncio
async def test_mcp_manager_lifecycle_and_cleanup_order(settings, log, monkeypatch):
    import avrone.mcp_client as mc

    class Mgr:
        def __init__(self, settings):
            log.append("mgr.init")

        async def connect_all(self, registry):  # first matching start-ish method wins
            log.append("mgr.connect_all")

        async def connect(self):
            log.append("mgr.connect (must not run)")

        def register_all(self, registry):
            log.append("mgr.register_all")

        async def aclose(self):
            log.append("mgr.aclose")

        def close(self):
            log.append("mgr.close (must not run)")

        async def status(self):
            return {"srv": "up"}
    monkeypatch.setattr(mc, "MCPManager", Mgr)
    core, cleanups = await bs.build_core(settings)
    assert core.mcp.__class__ is Mgr
    assert core.extensions["mcp"]["servers"] == {"srv": "up"}
    assert log == ["establish", "mgr.init", "mgr.connect_all", "mgr.register_all"]
    await bs.run_cleanups(cleanups)
    # reverse order: MCP closes before the runner it may depend on
    assert log[-2:] == ["mgr.aclose", "runner.stop"]


@pytest.mark.asyncio
async def test_mcp_status_attribute_and_servers_fallback(settings, log, monkeypatch):
    import avrone.mcp_client as mc

    class WithAttr:
        status = {"a": 1}

    class WithServers:
        status = None
        servers = ["s1"]

    monkeypatch.setattr(mc, "MCPManager", WithAttr)
    core, c1 = await bs.build_core(settings)
    assert core.extensions["mcp"]["servers"] == {"a": 1}
    await bs.run_cleanups(c1)
    monkeypatch.setattr(mc, "MCPManager", WithServers)
    core, c2 = await bs.build_core(settings)
    assert core.extensions["mcp"]["servers"] == ["s1"]
    assert len(c2) == 1  # no close method -> no extra cleanup
    await bs.run_cleanups(c2)


@pytest.mark.asyncio
async def test_mcp_failure_after_partial_start(settings, log, monkeypatch):
    import avrone.mcp_client as mc

    class Mgr:
        async def start(self):
            raise RuntimeError("server died")
    monkeypatch.setattr(mc, "MCPManager", Mgr)
    core, cleanups = await bs.build_core(settings)
    assert core.extensions["mcp"]["error"] == "RuntimeError: server died"
    assert "mcp: RuntimeError: server died" in core.extensions["errors"]
    await bs.run_cleanups(cleanups)


@pytest.mark.asyncio
async def test_fleet_failure_isolated(settings, log, monkeypatch):
    import avrone.fleet as fl

    class BadFleet:
        def __init__(self, *a, **k):
            raise ValueError("bad node id")
    monkeypatch.setattr(fl, "FleetManager", BadFleet)
    core, cleanups = await bs.build_core(settings)
    assert core.extensions["fleet"] == {"error": "ValueError: bad node id"}
    assert "fleet: ValueError: bad node id" in core.extensions["errors"]
    await bs.run_cleanups(cleanups)


@pytest.mark.asyncio
async def test_fleet_enabled_registers_and_cleans_up(settings, log, monkeypatch):
    import avrone.fleet as fl

    class FakeFleet:
        enabled = True

        def __init__(self, core, data_dir):
            log.append("fleet.init")

        def start_queue(self, data_dir):
            log.append("fleet.queue")

        def start(self):
            log.append("fleet.start")

        async def stop(self):
            log.append("fleet.stop")

        def info(self):
            return {"enabled": True}
    registered = []
    monkeypatch.setattr(fl, "FleetManager", FakeFleet)
    monkeypatch.setattr(fl, "register_fleet_tools", lambda reg: registered.append(reg))
    core, cleanups = await bs.build_core(settings)
    assert registered == [core.tools]
    assert core.extensions["fleet"] == {"enabled": True}
    await bs.run_cleanups(cleanups)
    assert log.index("fleet.stop") < log.index("runner.stop")


@pytest.mark.asyncio
async def test_runner_failure_propagates(settings, monkeypatch):
    async def broken(_s):
        raise RuntimeError("Environment health check failed")
    monkeypatch.setattr(bs, "establish", broken)
    with pytest.raises(RuntimeError, match="health check"):
        await bs.build_core(settings)


# ------------------------------------------------------------------ run_cleanups

@pytest.mark.asyncio
async def test_run_cleanups_reverse_order_and_failure_isolation():
    order: list[str] = []

    def a():
        order.append("a")

    async def b():
        order.append("b")
        raise RuntimeError("b failed")

    def c():
        order.append("c")
        raise OSError("c failed")

    async def d():
        order.append("d")

    await bs.run_cleanups([a, b, c, d])
    assert order == ["d", "c", "b", "a"]


@pytest.mark.asyncio
async def test_run_cleanups_empty():
    await bs.run_cleanups([])
