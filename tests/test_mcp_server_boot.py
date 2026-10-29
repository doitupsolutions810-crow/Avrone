"""Offline tests for avrone.mcp_server boot paths: build_core wiring, stdio entry points."""
import asyncio
import io
import logging
import sys

import pytest

from avrone import mcp_server as ms


def run(c):
    return asyncio.run(c)


class _Rec:
    """Stand-ins for the heavy collaborators build_core wires together."""

    def __init__(self):
        self.calls = []


@pytest.fixture
def boot(monkeypatch, tmp_path):
    """Patch every collaborator build_core imports lazily, so it runs fully offline."""
    import avrone.codex as codex_mod
    import avrone.llm as llm_mod
    import avrone.mcp_client as mcp_client_mod
    import avrone.plugins as plugins_mod
    import avrone.sovereign as sovereign_mod
    import avrone.trinity as trinity_mod
    from avrone.config import settings

    rec = _Rec()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "workspace", tmp_path / "ws")
    monkeypatch.delenv(ms._CHILD_ENV, raising=False)

    runner = object()

    async def fake_establish(s):
        rec.calls.append(("establish", s))
        return runner

    class FakeCore:
        def __init__(self, s, llm, r, codex):
            self.tools = "TOOLS"
            self.args = (s, llm, r, codex)

    class FakeMgr:
        fail = False

        async def start(self, tools):
            rec.calls.append(("mcp.start", tools))
            if FakeMgr.fail:
                raise RuntimeError("no mcp.json")

    def fake_load_plugins(tools, s):
        rec.calls.append(("plugins", tools))
        if rec.__dict__.get("plugin_fail"):
            raise RuntimeError("bad plugin dir")
        return []

    monkeypatch.setattr(sovereign_mod, "establish", fake_establish)
    monkeypatch.setattr(trinity_mod, "TrinityCore", FakeCore)
    monkeypatch.setattr(llm_mod, "LLM", lambda s: ("LLM", s))
    monkeypatch.setattr(codex_mod, "Codex", lambda d: ("CODEX", d))
    monkeypatch.setattr(plugins_mod, "load_plugins", fake_load_plugins)
    monkeypatch.setattr(mcp_client_mod, "MCPManager", FakeMgr)
    rec.runner, rec.settings, rec.Mgr, rec.tmp = runner, settings, FakeMgr, tmp_path
    yield rec
    # build_core sets the child marker in os.environ directly; never leak it to other tests
    import os
    os.environ.pop(ms._CHILD_ENV, None)


def test_build_core_wires_runner_plugins_and_mcp(boot):
    core, extras = run(ms.build_core())
    assert (boot.tmp / "data").is_dir() and (boot.tmp / "ws").is_dir()
    s, llm, r, codex = core.args
    assert s is boot.settings and r is boot.runner
    assert llm == ("LLM", boot.settings) and codex == ("CODEX", boot.tmp / "data")
    assert extras["runner"] is boot.runner
    assert isinstance(extras["mcp"], boot.Mgr)
    assert [c[0] for c in boot.calls] == ["establish", "plugins", "mcp.start"]
    import os
    assert os.environ[ms._CHILD_ENV] == "1"  # nested AVRONE children will skip MCP loading


def test_build_core_skips_mcp_when_running_as_child(boot, monkeypatch):
    monkeypatch.setenv(ms._CHILD_ENV, "1")
    core, extras = run(ms.build_core())
    assert extras["mcp"] is None
    assert "mcp.start" not in [c[0] for c in boot.calls]


def test_build_core_survives_plugin_and_mcp_failures(boot, caplog):
    boot.plugin_fail = True
    boot.Mgr.fail = True
    with caplog.at_level(logging.WARNING, logger="avrone.mcp_server"):
        core, extras = run(ms.build_core())
    assert core.tools == "TOOLS"
    assert extras["mcp"] is None  # failed start never recorded as a live manager
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "plugins not loaded" in msgs and "MCP servers not loaded" in msgs


def test_amain_redirects_stdout_and_serves_stdin(monkeypatch):
    seen = {}

    class FakeServer:
        async def serve(self, reader, out):
            seen["reader"], seen["out"] = reader, out
            seen["stdout_during"] = sys.stdout

    async def fake_reader():
        return "READER"

    fake_stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(ms, "AvroneMCPServer", FakeServer)
    monkeypatch.setattr(ms, "_stdin_reader", fake_reader)
    run(ms.amain())
    assert seen["reader"] == "READER"
    assert seen["out"] is fake_stdout.buffer           # JSON-RPC goes to the raw stdout buffer
    assert seen["stdout_during"] is sys.stderr          # stray prints are diverted to stderr


def test_main_swallows_keyboard_interrupt_and_sets_log_level(monkeypatch):
    configured = {}

    def fake_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setenv("AVRONE_LOG_LEVEL", "debug")
    monkeypatch.setattr(ms.logging, "basicConfig", lambda **kw: configured.update(kw))
    monkeypatch.setattr(ms.asyncio, "run", fake_run)
    ms.main()  # must not raise
    assert configured["level"] == "DEBUG" and configured["stream"] is sys.stderr


def test_stdin_reader_connects_read_pipe(monkeypatch):
    captured = {}

    async def go():
        loop = asyncio.get_running_loop()

        async def fake_connect(factory, pipe):
            proto = factory()
            captured["proto"], captured["pipe"] = proto, pipe
            return None, proto

        monkeypatch.setattr(loop, "connect_read_pipe", fake_connect)
        return await ms._stdin_reader()

    reader = run(go())
    assert isinstance(reader, asyncio.StreamReader)
    assert isinstance(captured["proto"], asyncio.StreamReaderProtocol)
    assert captured["pipe"] is sys.stdin
    assert reader._limit == 16 * 1024 * 1024  # large tool results must fit in one line
