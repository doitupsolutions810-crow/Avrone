"""Offline tests: the deep `rounds` knob is clamped to 1..5 and forwarded on every surface
(web /api/chat, MCP avrone_deep, CLI, fleet remote deep), and omitting it defaults to 1."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from avrone import cli, mcp_server as ms
from avrone.fleet import FleetManager, t_remote_spawn
from avrone.trinity import MAX_DEEP_ROUNDS, clamp_deep_rounds, resolve_deep_rounds

_MISSING = object()


@pytest.fixture(autouse=True)
def _no_env_rounds(monkeypatch):
    monkeypatch.delenv("AVRONE_DEEP_ROUNDS", raising=False)


class FakeDeepCore:
    """Records deep() calls; resolves rounds like the real core so 'omitted -> 1' is checkable."""

    def __init__(self):
        self.calls = []
        self.tools = SimpleNamespace(list=lambda: [], names=lambda: [])
        self.settings = SimpleNamespace()
        self.codex = MagicMock()
        self.active = {}

    async def deep(self, task, first_answer=None, n=None, run_id=None, rounds=_MISSING):
        self.calls.append({"task": task, "first": first_answer, "n": n, "rounds": rounds})
        eff = resolve_deep_rounds(None if rounds is _MISSING else rounds, self.settings)
        return {"answer": "deep", "report": "deep report", "run_id": run_id or "r", "max_rounds": eff}

    async def squad(self, task, n=None, run_id=None):
        return {"answer": "squad", "run_id": run_id or "r"}

    async def chat(self, task, run_id=None):
        return {"answer": "chat", "run_id": run_id or "r"}


CASES = [(1, 1), (3, 3), (5, 5), (0, 1), (-7, 1), (6, 5), (99, 5), ("4", 4)]


# ---------------------------------------------------------------- helper
@pytest.mark.parametrize("raw,want", CASES + [(None, None), ("", None), ("x", None), (True, None), ([], None)])
def test_clamp_deep_rounds(raw, want):
    assert clamp_deep_rounds(raw) == want
    assert MAX_DEEP_ROUNDS == 5


# ---------------------------------------------------------------- web server
@pytest.fixture()
def web(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        real = server.state["core"]
        fake = FakeDeepCore()
        server.state["core"] = fake
        try:
            yield c, fake
        finally:
            server.state["core"] = real


@pytest.mark.parametrize("raw,want", CASES)
def test_server_deep_clamps_and_forwards_rounds(web, raw, want):
    c, fake = web
    r = c.post("/api/chat", json={"message": "q", "mode": "deep", "first_answer": "a", "rounds": raw})
    assert r.status_code == 200, r.text
    assert fake.calls[-1]["rounds"] == want and r.json()["max_rounds"] == want
    assert fake.calls[-1]["first"] == "a"


def test_server_deep_omitted_rounds_defaults_to_one(web):
    c, fake = web
    r = c.post("/api/chat", json={"message": "q", "mode": "deep"})
    assert r.status_code == 200
    assert fake.calls[-1]["rounds"] is _MISSING and r.json()["max_rounds"] == 1
    r = c.post("/api/chat", json={"message": "q", "mode": "deep", "rounds": None})
    assert fake.calls[-1]["rounds"] is _MISSING and r.json()["max_rounds"] == 1


def test_server_rejects_non_int_rounds(web):
    c, _ = web
    assert c.post("/api/chat", json={"message": "q", "mode": "deep", "rounds": "lots"}).status_code == 422


def test_server_squad_mode_ignores_rounds(web):
    c, fake = web
    r = c.post("/api/chat", json={"message": "q", "mode": "squad", "rounds": 4})
    assert r.status_code == 200 and r.json()["answer"] == "squad" and not fake.calls


def test_server_real_mock_core_honours_rounds(tmp_path, monkeypatch):
    """End to end on the mock LLM: omitted -> max_rounds 1, oversized -> 5."""
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    if server.settings.llm_mode != "mock":
        pytest.skip("needs the mock LLM")
    with TestClient(server.app) as c:
        base = {"message": "q", "mode": "deep", "first_answer": "a", "squad_size": 1}
        assert c.post("/api/chat", json=base).json()["max_rounds"] == 1
        assert c.post("/api/chat", json={**base, "rounds": 40}).json()["max_rounds"] == 5


# ---------------------------------------------------------------- MCP server
def _mcp(core):
    async def factory():
        return core, {}
    return ms.AvroneMCPServer(core_factory=factory)


def test_mcp_deep_schema_advertises_rounds():
    deep = next(t for t in ms.TOOLS if t["name"] == "avrone_deep")
    prop = deep["inputSchema"]["properties"]["rounds"]
    assert prop["type"] == "integer" and prop["minimum"] == 1 and prop["maximum"] == 5
    assert "rounds" not in deep["inputSchema"]["required"]


@pytest.mark.parametrize("raw,want", CASES)
def test_mcp_deep_clamps_and_forwards_rounds(raw, want):
    core = FakeDeepCore()
    res = asyncio.run(_mcp(core).call_tool("avrone_deep", {"task": "t", "first_answer": "f", "rounds": raw}))
    assert res["isError"] is False
    assert core.calls[-1]["rounds"] == want and core.calls[-1]["first"] == "f"


@pytest.mark.parametrize("args", [{}, {"rounds": None}, {"rounds": "junk"}, {"rounds": True}])
def test_mcp_deep_omitted_or_junk_rounds_uses_default(args):
    core = FakeDeepCore()
    asyncio.run(_mcp(core).call_tool("avrone_deep", {"task": "t", **args}))
    assert core.calls[-1]["rounds"] is _MISSING


def test_mcp_deep_core_without_rounds_kw_still_works():
    class Legacy:
        tools = SimpleNamespace(list=lambda: [])

        async def deep(self, task, first=None):
            return {"answer": f"legacy:{task}:{first}"}
    res = asyncio.run(_mcp(Legacy()).call_tool("avrone_deep", {"task": "t"}))
    assert res["content"][0]["text"] == "legacy:t:None"


# ---------------------------------------------------------------- CLI
@pytest.mark.parametrize("argv,want", [([], _MISSING), (["--rounds", "3"], 3), (["--rounds", "40"], 5),
                                       (["--rounds", "0"], 1), (["--rounds", "-2"], 1)])
def test_cli_deep_clamps_and_forwards_rounds(monkeypatch, capsys, argv, want):
    core = FakeDeepCore()

    async def fake_build(settings, extras=True):
        return core, []
    monkeypatch.setattr(cli, "build_core", fake_build)
    assert cli.main(["deep", "q", "--first-answer", "a", "-q", *argv]) == 0
    assert core.calls[-1]["rounds"] == want
    assert "deep report" in capsys.readouterr().out


def test_cli_rounds_parser_rejects_non_int():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["deep", "q", "--rounds", "many"])


# ---------------------------------------------------------------- fleet
@pytest.fixture()
def fm(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    f = FleetManager(FakeDeepCore(), tmp_path)
    f.posted = []

    async def fake_post(url, path, payload, timeout=30):
        f.posted.append(payload)
        return {"node": "edge", "path": ["edge"], "answer": "remote", "run_id": "rr"}
    f._post = fake_post
    f.pick = lambda target: {"node_id": "edge", "url": "http://edge.invalid", "load": 0}
    return f


@pytest.mark.parametrize("raw,want", CASES)
def test_fleet_dispatch_clamps_rounds_into_payload(fm, raw, want):
    asyncio.run(fm.dispatch(["edge"], "deep", "p", 0, 2, "o", rounds=raw))
    assert fm.posted[-1]["rounds"] == want


def test_fleet_dispatch_omits_rounds_when_not_given(fm):
    asyncio.run(fm.dispatch(["edge"], "deep", "p", 0))
    asyncio.run(fm.dispatch(["edge"], "deep", "p", 0, rounds="junk"))
    assert all("rounds" not in p for p in fm.posted)


@pytest.mark.parametrize("raw,want", CASES + [(None, _MISSING)])
def test_fleet_handle_run_local_deep_forwards_rounds(fm, raw, want):
    payload = {"mode": "deep", "prompt": "p", "hops": 1}
    if raw is not None:
        payload["rounds"] = raw
    res = asyncio.run(fm.handle_run(payload))
    assert res["answer"] == "deep" and fm.core.calls[-1]["rounds"] == want


def test_fleet_handle_run_relays_rounds_down_route(fm):
    res = asyncio.run(fm.handle_run({"mode": "deep", "prompt": "p", "hops": 1, "route": ["edge"], "rounds": 9}))
    assert fm.posted[-1]["rounds"] == 5 and res["path"] == ["hub", "edge"]


def test_fleet_remote_spawn_tool_forwards_rounds(fm):
    fm.core.fleet = fm
    ctx = SimpleNamespace(core=fm.core, run_id="run1")
    out = asyncio.run(t_remote_spawn(ctx, {"task": "t", "node": "edge", "mode": "deep", "rounds": 0}))
    assert fm.posted[-1]["rounds"] == 1 and fm.posted[-1]["mode"] == "deep" and "remote" in out
    asyncio.run(t_remote_spawn(ctx, {"task": "t", "node": "edge", "mode": "deep"}))
    assert "rounds" not in fm.posted[-1]


def test_env_default_still_applies_when_rounds_omitted(monkeypatch):
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", "3")
    core = FakeDeepCore()
    res = asyncio.run(_mcp(core).call_tool("avrone_deep", {"task": "t"}))
    assert res["isError"] is False and core.calls[-1]["rounds"] is _MISSING
    assert resolve_deep_rounds(None) == 3 and os.environ["AVRONE_DEEP_ROUNDS"] == "3"
