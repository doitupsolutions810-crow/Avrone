import asyncio
import json
from types import SimpleNamespace

import pytest

from avrone import presence
from avrone import tools_extra as tx
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry

SECRET_VALUE = "sk-live-SUPERSECRET-123456"
TOGGLES = ("AVRONE_RECON", "AVRONE_RECON_ALLOWLIST", "AVRONE_SELF_EVOLVE", "AVRONE_SELF_CHANGES")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in TOGGLES:
        monkeypatch.delenv(k, raising=False)
    import platform
    import socket
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "release", lambda: "6.1-test")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "python_version", lambda: "3.12.9")
    monkeypatch.setattr(socket, "gethostname", lambda: "avrone-node-7")
    monkeypatch.setattr(presence, "_host_interfaces",
                        lambda: [{"name": "eth0", "state": "UP", "addrs": ["10.0.0.5/24"]},
                                 {"name": "lo", "state": "UNKNOWN", "addrs": ["127.0.0.1/8"]}])
    monkeypatch.setattr(tx.shutil, "which", lambda b: None)
    monkeypatch.chdir(tmp_path)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", agents_can_shell=False,
                 allow_network=False, runner="local", admin_token="")
    runner = LocalRunner(s)
    run(runner.start())
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    core = SimpleNamespace(settings=s, runner=runner, tools=reg)
    ctx = SimpleNamespace(core=core, run_id="r1", name="Worker-1", depth=0)
    return SimpleNamespace(ctx=ctx, reg=reg, s=s, tmp=tmp_path)


def call(env, **args):
    return run(env.reg.call(env.ctx, "self_scan", args))


def test_registered_builtin(env):
    t = env.reg.get("self_scan")
    assert t is not None and t.source == "builtin" and "format" in t.args_doc


def test_text_report_fields(env):
    out = call(env)
    assert "own environment only" in out
    assert "Linux 6.1-test x86_64 python 3.12.9 host=avrone-node-7" in out
    assert f"workspace: {env.s.workspace}" in out and "(writable=True)" in out
    assert f"cwd: {env.tmp}" in out
    assert "runner: local (configured=local) docker_available=False" in out
    for frag in ("AVRONE_RECON=allow", "AVRONE_RECON_ALLOWLIST=(any)", "AVRONE_ALLOW_NETWORK=False",
                 "AVRONE_AGENTS_CAN_SHELL=False", "AVRONE_SELF_EVOLVE=allow", "AVRONE_SELF_CHANGES=allow",
                 "admin_token=off"):
        assert frag in out
    assert f"tools registered: {len(env.reg.names())}" in out
    assert "eth0 UP 10.0.0.5/24" in out and "lo UNKNOWN 127.0.0.1/8" in out


def test_json_shape(env, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "DENY")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "example.com, .lab.local")
    monkeypatch.setenv("AVRONE_SELF_EVOLVE", "deny")
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "log")
    monkeypatch.setattr(tx.shutil, "which", lambda b: "/usr/bin/docker" if b == "docker" else None)
    env.s.admin_token = SECRET_VALUE
    d = json.loads(call(env, format="json"))
    assert set(d) == {"system", "paths", "runner", "toggles", "tools", "network_interfaces", "secrets"}
    assert d["toggles"] == {"AVRONE_RECON": "deny", "AVRONE_RECON_ALLOWLIST": "example.com,.lab.local",
                            "AVRONE_ALLOW_NETWORK": False, "AVRONE_AGENTS_CAN_SHELL": False,
                            "AVRONE_SELF_EVOLVE": "deny", "AVRONE_SELF_CHANGES": "log", "admin_token": "on"}
    assert d["runner"]["docker_available"] is True
    assert d["paths"]["workspace_writable"] is True and d["paths"]["workspace_disk"]["total_gib"] >= 0
    assert d["tools"]["registered"] == len(env.reg.names())
    assert SECRET_VALUE not in json.dumps(d)


def test_secrets_redacted(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_VALUE)
    monkeypatch.setenv("MY_DB_PASSWORD", SECRET_VALUE)
    monkeypatch.setenv("some_secret_thing", SECRET_VALUE)
    monkeypatch.setenv("GH_TOKEN", "")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    env.s.admin_token = SECRET_VALUE
    for out in (call(env), call(env, format="json")):
        assert SECRET_VALUE not in out
    d = json.loads(call(env, format="json"))["secrets"]
    assert d["OPENAI_API_KEY"] == "***set***" and d["MY_DB_PASSWORD"] == "***set***"
    assert d["some_secret_thing"] == "***set***"
    assert d["GH_TOKEN"] == "unset" and d["XAI_API_KEY"] == "unset"


def test_redact_env_helper():
    assert tx.redact_env("API_KEY", "x") == "***set***"
    assert tx.redact_env("Session_Token", None) == "unset"
    assert tx.redact_env("AVRONE_LLM_BASE_URL", "https://user:pw@h.example/v1") == "https://***@h.example/v1"
    assert tx.redact_env("PLAIN", None) == "unset"
    assert tx.redact_env("PLAIN", "a" * 500).endswith("...") and len(tx.redact_env("PLAIN", "a" * 500)) == 203


def test_bounded(env, monkeypatch):
    many = [{"name": f"if{i}", "state": "UP", "addrs": [f"10.{i}.0.{j}/32" for j in range(50)]} for i in range(100)]
    monkeypatch.setattr(presence, "_host_interfaces", lambda: many)
    for i in range(200):
        monkeypatch.setenv(f"ZZ_FAKE_{i:03d}_TOKEN", "v")
    d = json.loads(call(env, format="json"))
    assert len(d["network_interfaces"]) == tx.SELF_SCAN_MAX_IFACES
    assert all(len(i["addrs"]) <= tx.SELF_SCAN_MAX_ADDRS for i in d["network_interfaces"])
    assert len(d["secrets"]) <= tx.SELF_SCAN_MAX_SECRET_NAMES
    assert len(call(env)) <= tx.SELF_SCAN_MAX_CHARS + 20


def test_degrades_without_tools_or_interfaces(env, monkeypatch):
    def boom():
        raise RuntimeError("no ip")
    monkeypatch.setattr(presence, "_host_interfaces", boom)
    env.ctx.core = SimpleNamespace(settings=env.s, runner=env.ctx.core.runner)
    out = call(env)
    assert "tools registered: unknown" in out and "(none found)" in out


def test_readonly_does_not_write(env):
    before = sorted(p.name for p in env.tmp.rglob("*"))
    call(env)
    call(env, format="json")
    assert sorted(p.name for p in env.tmp.rglob("*")) == before


def test_no_outbound_network(env, monkeypatch):
    import socket

    def no_net(*a, **k):
        raise AssertionError("self_scan must not open sockets")
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(tx, "_client", no_net)
    assert "self_scan" in call(env)
