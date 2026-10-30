"""Offline edge-case tests for the Ghost Shell (avrone.server) plus tools.py / sovereign.py
local-runner edges. No real network: outbound HTTP is faked with httpx.MockTransport."""
import asyncio
import json
import os
import shlex
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from avrone.fleet import sign
from avrone.sovereign import LocalRunner

ADMIN = {"x-admin-token": "test-token"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


@pytest.fixture()
def fclient(tmp_path, monkeypatch):
    """Server with the fleet enabled (shared token) and a known node id."""
    from avrone import server
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "fleet-secret")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_MAX_HOPS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def signed_post(c, path, payload=None, raw=None, token="fleet-secret", **sign_kw):
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    h = {"content-type": "application/json", **sign(token, "POST", path, body, **sign_kw)}
    return c.post(path, content=body, headers=h)


# ---------------------------------------------------------------- admin auth
@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/terminal", {"cmd": "echo hi"}),
    ("get", "/api/fleet", None),
    ("get", "/api/fleet/directory", None),
    ("get", "/api/fleet/jobs", None),
    ("post", "/api/fleet/tell", {"agent": "A-1", "message": "m"}),
])
def test_admin_routes_reject_missing_and_wrong_token(client, method, path, body):
    kw = {"json": body} if body is not None else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers={"x-admin-token": "nope"}, **kw).status_code == 401


def test_admin_off_when_no_token_configured(client, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "admin_token", "")
    r = client.post("/api/terminal", json={"cmd": "echo hi"}, headers=ADMIN)
    assert r.status_code == 403 and "Admin access is off" in r.json()["detail"]
    assert client.get("/api/status").json()["admin_enabled"] is False


def test_terminal_ok_and_logged(client):
    r = client.post("/api/terminal", json={"cmd": "echo hello; exit 3"}, headers=ADMIN)
    assert r.status_code == 200
    d = r.json()
    assert d["exit_code"] == 3 and "hello" in d["stdout"] and d["timed_out"] is False
    log = client.get("/api/runs/admin").json()
    assert log[-1]["kind"] == "tool.result" and log[-1]["data"]["tool"] == "terminal"


# ---------------------------------------------------------------- bad bodies
@pytest.mark.parametrize("path,kw", [
    ("/api/chat", {"content": b"{not json", "headers": {"content-type": "application/json"}}),
    ("/api/chat", {"json": {}}),                                 # missing message
    ("/api/chat", {"json": {"message": "x", "squad_size": "lots"}}),
    ("/api/terminal", {"json": {"command": "ls"}, "headers": ADMIN}),
])
def test_bad_bodies_are_422(client, path, kw):
    assert client.post(path, **kw).status_code == 422


def test_unknown_mode_is_400(client):
    r = client.post("/api/chat", json={"message": "x", "mode": "yolo"})
    assert r.status_code == 400 and "Unknown mode" in r.json()["detail"]


@pytest.mark.parametrize("mode", ["chat", "deep"])
def test_chat_and_deep_modes(client, mode):
    r = client.post("/api/chat", json={"message": "hi", "mode": mode, "first_answer": "draft", "squad_size": 2})
    assert r.status_code == 200 and r.json().get("run_id")


def test_background_run_returns_immediately(client):
    r = client.post("/api/chat", json={"message": "hi", "mode": "chat", "background": True}).json()
    assert r["status"] == "running" and len(r["run_id"]) == 10
    for _ in range(50):  # let the background task finish so the run log exists
        if client.get(f"/api/runs/{r['run_id']}").status_code == 200:
            break
        time.sleep(0.05)
    assert any(x["run_id"] == r["run_id"] for x in client.get("/api/runs").json())


# ---------------------------------------------------------------- runs
def test_unknown_run_id_is_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_run_id_traversal_is_404(client):
    assert client.get("/api/runs/..%2F..%2Fetc%2Fpasswd").status_code == 404


# ---------------------------------------------------------------- misc read endpoints
def test_read_endpoints(client):
    assert client.get("/").status_code == 200
    names = {t["name"] for t in client.get("/api/tools").json()}
    assert {"shell", "read_file", "write_file", "fetch", "spawn"} <= names
    assert isinstance(client.get("/api/extensions").json(), dict)
    assert client.get("/api/gateway").status_code == 200
    assert "accepted" in client.get("/api/evolve").json()
    assert "metrics" in client.get("/api/osint/metrics").json()
    assert client.get("/api/telemetry?hours=1").status_code == 200
    assert isinstance(client.get("/api/map").json(), dict)
    r = client.get("/api/map?format=mermaid")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------- websocket
def test_websocket_streams_events_and_unsubscribes(client):
    from avrone import server
    codex = server.core().codex
    before = len(codex._subscribers)
    with client.websocket_connect("/ws/events") as ws:
        for _ in range(50):
            if len(codex._subscribers) > before:
                break
            time.sleep(0.01)
        client.post("/api/terminal", json={"cmd": "echo ws"}, headers=ADMIN)
        ev = ws.receive_json()
        assert ev["run_id"] == "admin" and ev["kind"] == "tool.result"
    for _ in range(50):
        if len(codex._subscribers) == before:
            break
        time.sleep(0.01)
    assert len(codex._subscribers) == before


# ---------------------------------------------------------------- fleet endpoints (disabled)
@pytest.mark.parametrize("path", ["/api/fleet/register", "/api/fleet/message", "/api/fleet/run"])
def test_fleet_signed_routes_404_when_disabled(client, path):
    assert signed_post(client, path, {}).status_code == 404


def test_fleet_admin_routes_404_when_disabled(client):
    assert client.get("/api/fleet", headers=ADMIN).status_code == 404
    assert client.get("/api/fleet/jobs", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------- fleet endpoints (enabled)
def test_fleet_info_and_register(fclient):
    info = fclient.get("/api/fleet", headers=ADMIN).json()
    assert info["node_id"] == "hub" and info["enabled"] and info["nodes"] == []
    r = signed_post(fclient, "/api/fleet/register", {"node_id": "edge1", "url": "http://edge1.test"})
    assert r.json() == {"ok": True, "parent": "hub"}
    nodes = fclient.get("/api/fleet", headers=ADMIN).json()["nodes"]
    assert nodes[0]["node_id"] == "edge1" and nodes[0]["status"] == "online"


@pytest.mark.parametrize("payload,code", [
    ({"node_id": "hub", "url": "http://x"}, 400),           # collides with this server
    ({"node_id": "BAD ID", "url": "http://x"}, 400),
    ({"node_id": "edge2", "url": "gopher://x"}, 400),
])
def test_fleet_register_rejects(fclient, payload, code):
    assert signed_post(fclient, "/api/fleet/register", payload).status_code == code


def test_fleet_register_allowlist_403(fclient):
    from avrone import server
    server.core().fleet.allow = {"only-me"}
    r = signed_post(fclient, "/api/fleet/register", {"node_id": "edge1", "url": "http://e"})
    assert r.status_code == 403


def test_fleet_auth_failures_are_401(fclient):
    p = "/api/fleet/register"
    body = {"node_id": "e", "url": "http://e"}
    assert fclient.post(p, json=body).status_code == 401                                  # unsigned
    assert signed_post(fclient, p, body, token="wrong").status_code == 401                 # bad sig
    old = str(int(time.time()) - 600)
    r = signed_post(fclient, p, body, ts=old)
    assert r.status_code == 401 and "stale" in r.json()["detail"]
    raw = json.dumps(body).encode()
    h = {"content-type": "application/json", **sign("fleet-secret", "POST", p, raw)}
    assert fclient.post(p, content=raw, headers=h).status_code == 200
    r = fclient.post(p, content=raw, headers=h)                                            # replay
    assert r.status_code == 401 and "replayed" in r.json()["detail"]
    # signature over a different path is rejected
    h = {"content-type": "application/json", **sign("fleet-secret", "POST", "/api/fleet/run", raw)}
    assert fclient.post(p, content=raw, headers=h).status_code == 401


@pytest.mark.parametrize("path", ["/api/fleet/register", "/api/fleet/message", "/api/fleet/run"])
def test_fleet_signed_but_malformed_json_is_400(fclient, path):
    for raw in (b"{oops", b"[1, 2]"):
        r = signed_post(fclient, path, raw=raw)
        assert r.status_code == 400, (raw, r.status_code, r.text)


def test_fleet_message_errors_and_local_delivery(fclient, monkeypatch):
    from avrone import server
    c = server.core()
    r = signed_post(fclient, "/api/fleet/message", {"agent": "A", "message": "m", "hops": 99})
    assert r.status_code == 403
    r = signed_post(fclient, "/api/fleet/message", {"hops": 1})            # missing agent -> KeyError
    assert r.status_code == 502
    r = signed_post(fclient, "/api/fleet/message", {"agent": "A", "message": "m", "route": ["ghost"]})
    assert r.status_code == 502 and "unknown node" in r.json()["detail"]
    monkeypatch.setattr(c, "message_agent", lambda *a: True)
    r = signed_post(fclient, "/api/fleet/message", {"agent": "Builder-1", "message": "m"})
    assert r.status_code == 200 and r.json()["delivered"] == "live"


def test_fleet_run_paths(fclient, monkeypatch):
    from avrone import server
    c = server.core()
    assert signed_post(fclient, "/api/fleet/run", {"hops": 99}).status_code == 403
    r = signed_post(fclient, "/api/fleet/run", {"route": ["ghost"], "hops": 1})
    assert r.status_code == 502
    r = signed_post(fclient, "/api/fleet/run", {"mode": "chat", "prompt": "hello", "hops": 1})
    assert r.status_code == 200 and r.json()["node"] == "hub" and r.json()["answer"]


def test_fleet_tell(fclient, monkeypatch):
    from avrone import server
    import avrone.presence as presence
    c = server.core()
    r = fclient.post("/api/fleet/tell", json={"agent": "nowhere/Builder-1", "message": "m"}, headers=ADMIN)
    assert r.status_code == 404
    monkeypatch.setattr(presence, "find_agent", lambda rows, ref: ("ghost", "Builder-1"))
    r = fclient.post("/api/fleet/tell", json={"agent": "x", "message": "m"}, headers=ADMIN)
    assert r.status_code == 502
    monkeypatch.setattr(presence, "find_agent", lambda rows, ref: ("", "Builder-1"))
    monkeypatch.setattr(c, "message_agent", lambda *a: True)
    r = fclient.post("/api/fleet/tell", json={"agent": "Builder-1", "message": "m"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["delivered"] == "live"


def test_fleet_directory_and_jobs(fclient):
    rows = fclient.get("/api/fleet/directory", headers=ADMIN).json()
    assert rows and rows[0]["node_id"] == "hub"
    assert isinstance(fclient.get("/api/fleet/jobs", headers=ADMIN).json(), dict)
    assert fclient.get("/api/fleet/jobs?job_id=nope", headers=ADMIN).status_code == 404
    from avrone import server
    q = server.core().fleet.queue
    jid = fclient.portal.call(q.enqueue, "run", {"task": "t", "node": "ghost"}, 0, 1)
    j = fclient.get(f"/api/fleet/jobs?job_id={jid}", headers=ADMIN).json()
    assert j["id"] == jid


def test_server_main_invokes_uvicorn(monkeypatch):
    from avrone import server
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(app=app, host=host, port=port))
    monkeypatch.setenv("AVRONE_PORT", "9999")
    monkeypatch.delenv("AVRONE_HOST", raising=False)
    server.main()
    assert seen == {"app": "avrone.server:app", "host": "127.0.0.1", "port": 9999}


# ---------------------------------------------------------------- tools.py edges
def run(coro):
    return asyncio.run(coro)


def tctx(tmp_path, **settings):
    s = SimpleNamespace(agents_can_shell=True, allow_network=True, max_spawn_depth=2, **settings)
    from avrone.sovereign import CommandResult
    runner = SimpleNamespace(workspace=tmp_path, run=AsyncMock(return_value=CommandResult("c", 0, "out", "")))
    core = SimpleNamespace(settings=s, runner=runner, run_agent=AsyncMock(return_value="sub done"))
    return SimpleNamespace(core=core, depth=0, run_id="r", name="Lead-1")


def test_registry_rejects_bad_names_and_reports_errors(tmp_path):
    from avrone.tools import Tool, ToolRegistry
    r = ToolRegistry()
    with pytest.raises(ValueError, match="bad tool name"):
        r.register(Tool("rm -rf", "d", "{}", AsyncMock()))

    async def boom(ctx, args):
        raise KeyError("cmd")
    r.register(Tool("boom", "explodes", "{}", boom))
    assert run(r.call(None, "boom", {})) == "ERROR: KeyError: 'cmd'"
    assert run(r.call(None, "nope", {})).startswith("ERROR: unknown tool 'nope'. Known: boom")
    assert "- boom {}: explodes" in r.docs()
    r.unregister("boom")
    r.unregister("boom")  # idempotent
    assert r.names() == []


def test_shell_disabled_and_install_ledger(tmp_path, monkeypatch):
    from avrone import tools
    ctx = tctx(tmp_path)
    ctx.core.settings.agents_can_shell = False
    assert "disabled" in run(tools.t_shell(ctx, {"cmd": "ls"}))
    ctx.core.settings.agents_can_shell = True
    seen = []
    import avrone.agentkit as ak
    monkeypatch.setattr(ak, "record_change", lambda *a: seen.append(a))
    assert "out" in run(tools.t_shell(ctx, {"cmd": "pip install requests"}))
    assert seen and seen[0][1] == "shell install" and seen[0][3] is True
    seen.clear()
    run(tools.t_shell(ctx, {"cmd": "echo pip"}))
    assert not seen
    # a failing ledger never breaks the shell call
    monkeypatch.setattr(ak, "record_change", lambda *a: (_ for _ in ()).throw(RuntimeError("ledger down")))
    assert "out" in run(tools.t_shell(ctx, {"cmd": "npm install left-pad"}))


def test_read_write_path_safety(tmp_path):
    from avrone import tools
    ctx = tctx(tmp_path)
    assert run(tools.t_write(ctx, {"path": "/a/b.txt", "content": "hi"})).startswith("wrote a/b.txt (2 bytes)")
    assert run(tools.t_read(ctx, {"path": "a/b.txt"})) == "hi"
    assert "not found" in run(tools.t_read(ctx, {"path": "missing.txt"}))
    for bad in ("../escape.txt", "a/../../escape.txt"):
        with pytest.raises(ValueError, match="escapes"):
            run(tools.t_write(ctx, {"path": bad, "content": "x"}))
        with pytest.raises(ValueError, match="escapes"):
            run(tools.t_read(ctx, {"path": bad}))
    assert not (tmp_path.parent / "escape.txt").exists()


def test_fetch_guards_and_html_stripping(tmp_path, monkeypatch):
    from avrone import tools
    ctx = tctx(tmp_path)
    ctx.core.settings.allow_network = False
    assert "disabled" in run(tools.t_fetch(ctx, {"url": "http://x"}))
    ctx.core.settings.allow_network = True
    assert "must start with" in run(tools.t_fetch(ctx, {"url": "file:///etc/passwd"}))
    html = "<html><style>p{}</style><script>evil()</script><h1>Hi</h1>\n\n<p>there</p></html>"
    real = httpx.AsyncClient

    def fake_client(**kw):
        kw["transport"] = httpx.MockTransport(lambda req: httpx.Response(201, text=html))
        return real(**kw)
    monkeypatch.setattr(tools.httpx, "AsyncClient", fake_client)
    out = run(tools.t_fetch(ctx, {"url": "https://example.test"}))
    assert out == "[HTTP 201]  Hi there "


def test_spawn_depth_limit(tmp_path):
    from avrone import tools
    ctx = tctx(tmp_path)
    ctx.depth = 2
    assert "max sub-agent depth" in run(tools.t_spawn(ctx, {"task": "t"}))
    ctx.depth = 0
    assert run(tools.t_spawn(ctx, {"task": "t"})) == "sub done"
    kw = ctx.core.run_agent.call_args.kwargs
    assert kw["role"] == "Helper" and kw["depth"] == 1 and kw["parent"] == "Lead-1"


# ---------------------------------------------------------------- sovereign.py edges
def rsettings(tmp_path, **kw):
    base = dict(workspace=tmp_path / "ws", command_timeout=5, runner="local", allow_network=False,
                docker_image="img")
    base.update(kw)
    return SimpleNamespace(**base)


def test_command_result_text_truncation_and_stderr():
    from avrone.sovereign import CommandResult
    r = CommandResult("c", 1, "o" * 100, "bad", timed_out=True)
    t = r.text(limit=40)
    assert t.startswith("$ c\n[exit 1 TIMEOUT]") and t.endswith("...[truncated]")
    assert "[stderr]\nbad" in CommandResult("c", 1, "", "bad").text()


def test_local_runner_timeout_and_cwd(tmp_path):
    from avrone.sovereign import LocalRunner, Runner
    s = rsettings(tmp_path)
    r = LocalRunner(s)

    async def go():
        await r.start()
        res = await r.run("pwd")
        assert res.stdout.strip() == str(s.workspace)
        res = await r.run("sleep 5", timeout=0.3)
        assert res.timed_out and res.exit_code == -1
        res = await r.run("echo err >&2; exit 4")
        assert res.exit_code == 4 and res.stderr.strip() == "err"
    run(go())
    with pytest.raises(NotImplementedError):
        run(Runner(s).run("x"))


def test_docker_runner_argv_and_failure(tmp_path, monkeypatch):
    from avrone.sovereign import CommandResult, DockerRunner
    calls = []
    results = [CommandResult("x", 1, "", "no daemon")]

    async def fake_exec(self, argv, cmd, timeout, cwd=None):
        calls.append(argv)
        return results.pop(0) if results else CommandResult(cmd, 0, "ok", "")
    monkeypatch.setattr(DockerRunner, "_exec", fake_exec)
    d = DockerRunner(rsettings(tmp_path))
    with pytest.raises(RuntimeError, match="no daemon"):
        run(d.start())
    assert "--network" in calls[0] and calls[0][-3:] == ["img", "sleep", "infinity"]
    d2 = DockerRunner(rsettings(tmp_path, allow_network=True))
    run(d2.start())
    assert "--network" not in calls[-1]
    assert run(d2.run("ls")).stdout == "ok" and calls[-1][:3] == ["docker", "exec", d2.container]
    run(d2.stop())
    assert calls[-1][:3] == ["docker", "rm", "-f"]


def test_establish_auto_falls_back_to_local(tmp_path, monkeypatch):
    from avrone import sovereign
    monkeypatch.setattr(sovereign.shutil, "which", lambda n: "/usr/bin/docker")

    async def bad_start(self):
        raise RuntimeError("docker broken")
    monkeypatch.setattr(sovereign.DockerRunner, "start", bad_start)
    r = run(sovereign.establish(rsettings(tmp_path, runner="auto")))
    assert r.kind == "local"
    # explicit docker choice does not silently fall back
    with pytest.raises(RuntimeError, match="docker broken"):
        run(sovereign.establish(rsettings(tmp_path, runner="docker")))
    # auto without docker on PATH picks local directly
    monkeypatch.setattr(sovereign.shutil, "which", lambda n: None)
    assert run(sovereign.establish(rsettings(tmp_path, runner="auto"))).kind == "local"


def test_establish_health_check_failure(tmp_path, monkeypatch):
    from avrone import sovereign

    async def failing(self, cmd, timeout=None):
        return sovereign.CommandResult(cmd, 1, "", "broken shell")
    monkeypatch.setattr(sovereign.LocalRunner, "run", failing)
    with pytest.raises(RuntimeError, match="health check failed"):
        run(sovereign.establish(rsettings(tmp_path)))



# ---------------------------------------------------------------- subprocess cleanup
@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_local_runner_timeout_kills_descendant_processes(tmp_path):
    marker = tmp_path / "timeout-child-survived"
    runner = LocalRunner(SimpleNamespace(workspace=tmp_path, command_timeout=120))
    cmd = f"(sleep 0.35; touch {shlex.quote(str(marker))}) &"
    result = asyncio.run(runner.run(cmd, timeout=0.05))
    assert result.timed_out
    time.sleep(0.45)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_local_runner_cancellation_kills_descendant_processes(tmp_path):
    marker = tmp_path / "cancel-child-survived"
    runner = LocalRunner(SimpleNamespace(workspace=tmp_path, command_timeout=120))
    cmd = f"(sleep 0.35; touch {shlex.quote(str(marker))}) &"

    async def exercise():
        task = asyncio.create_task(runner.run(cmd, timeout=5))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.45)

    asyncio.run(exercise())
    assert not marker.exists()
