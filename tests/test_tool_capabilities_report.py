import asyncio
import json
from types import SimpleNamespace

import pytest

from avrone import presence
from avrone import tools_extra as tx
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry

SECRET_VALUE = "tok-DO-NOT-LEAK-987654"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_RECON", "AVRONE_RECON_ALLOWLIST", "AVRONE_SELF_EVOLVE", "AVRONE_SELF_CHANGES"):
        monkeypatch.delenv(k, raising=False)
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "node-x")
    monkeypatch.setattr(presence, "_host_interfaces", lambda: [{"name": "lo", "state": "UP", "addrs": ["127.0.0.1/8"]}])
    monkeypatch.setattr(tx.shutil, "which", lambda b: None)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", agents_can_shell=True,
                 allow_network=True, runner="local", admin_token="")
    runner = LocalRunner(s)
    run(runner.start())
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=s, runner=runner, tools=reg), run_id="r", name="W", depth=0)
    return SimpleNamespace(ctx=ctx, reg=reg, s=s)


def call(env, **args):
    return run(env.reg.call(env.ctx, "capabilities_report", args))


def ledger(env):
    return {r["capability"]: r for r in json.loads(call(env, format="json"))["ledger"]}


def test_registered(env):
    t = env.reg.get("capabilities_report")
    assert t is not None and t.source == "builtin" and "format" in t.args_doc


def test_defaults_text(env):
    out = call(env)
    assert out.startswith("capabilities_report for node-x")
    for frag in ("- shell: enabled", "- outbound network: enabled", "- recon: allow (allowlist=(any))",
                 "- self-evolve: allow", "- self-changes: allow", "- workspace write: enabled",
                 "- admin API: off", "- docker: unavailable (no docker binary on this host)", "- runner: local"):
        assert frag in out
    can = next(line for line in out.splitlines() if line.startswith("CAN:"))
    cannot = next(line for line in out.splitlines() if line.startswith("CANNOT:"))
    assert "shell" in can and "outbound network" in can and "recon" in can
    assert "docker" in cannot and "admin API" in cannot


def test_locked_down(env, monkeypatch):
    env.s.agents_can_shell = False
    env.s.allow_network = False
    monkeypatch.setenv("AVRONE_RECON", "deny")
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "deny")
    monkeypatch.setattr(tx.shutil, "which", lambda b: "/usr/bin/docker")
    led = ledger(env)
    for cap in ("shell", "docker", "outbound network", "recon", "self-evolve", "self-changes"):
        assert led[cap]["can"] is False, cap
    assert "AVRONE_AGENTS_CAN_SHELL=false" in led["shell"]["detail"]
    assert led["docker"]["detail"] == "AVRONE_AGENTS_CAN_SHELL=false"
    assert led["recon"]["status"] == "deny" and led["self-changes"]["detail"] == "env_install refused"
    out = call(env)
    assert "CAN: workspace write, runner" in out


def test_allowlist_log_docker_and_admin(env, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com,lab.local")
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "log")
    monkeypatch.setattr(tx.shutil, "which", lambda b: "/usr/bin/docker")
    env.s.allow_network = False
    env.s.admin_token = SECRET_VALUE
    monkeypatch.setenv("SOME_API_KEY", SECRET_VALUE)
    led = ledger(env)
    assert led["recon"]["detail"] == "allowlist=mysite.com,lab.local; outbound network is off"
    assert led["self-changes"] == {"capability": "self-changes", "can": True, "status": "log",
                                   "detail": "env_install runs and is logged"}
    assert led["docker"]["can"] is True and "--network none" in led["docker"]["detail"]
    assert led["admin API"]["can"] is True and "value hidden" in led["admin API"]["detail"]
    assert led["credentials"]["can"] is True
    for out in (call(env), call(env, format="json")):
        assert SECRET_VALUE not in out


def test_json_shape(env):
    d = json.loads(call(env, format="json"))
    assert set(d) == {"host", "tools_registered", "ledger"}
    assert d["tools_registered"] == len(env.reg.names())
    assert all(set(r) == {"capability", "can", "status", "detail"} for r in d["ledger"])
    assert [r["capability"] for r in d["ledger"]] == [
        "shell", "docker", "outbound network", "recon", "self-evolve", "self-changes",
        "workspace write", "admin API", "runner", "credentials"]


def test_readonly_workspace(env, monkeypatch):
    real = tx.os.access
    monkeypatch.setattr(tx.os, "access", lambda p, m: False if m == tx.os.W_OK else real(p, m))
    led = ledger(env)
    assert led["workspace write"] == {"capability": "workspace write", "can": False, "status": "read-only",
                                      "detail": str(env.s.workspace)}
