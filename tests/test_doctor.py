"""avrone.doctor: offline, read-only hosting preflight. No network, no subprocess, no root."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import doctor as d
from avrone.cli import main as cli_main

SECRET = "s3cr3t-value-that-must-never-leak-0123456789"


def _settings(tmp_path: Path, **kw):
    base = dict(provider="openclaw", llm_mode="mock", llm_base_url="http://127.0.0.1:18789/v1",
                llm_api_key="", data_dir=tmp_path / "data", workspace=tmp_path / "ws", runner="docker",
                allow_network=False, agents_can_shell=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _safe_env(**kw):
    env = {"AVRONE_RECON": "deny", "AVRONE_HOST": "127.0.0.1"}
    env.update(kw)
    return env


def _docker(_name):
    return "/usr/bin/docker"


def _nodocker(_name):
    return None


def _by_name(report):
    out: dict[str, list[dict]] = {}
    for c in report["checks"]:
        out.setdefault(c["name"], []).append(c)
    return out


def _sock(tmp_path):
    s = tmp_path / "docker.sock"
    s.write_text("")
    return (str(s),)


# ------------------------------------------------------------------ basics

def test_clean_locked_down_node_is_ok(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_ADMIN_TOKEN="a" * 32),
                       which=_docker, sockets=_sock(tmp_path), python_version=(3, 12, 1))
    assert rep["ok"] and rep["status"] == "ok", d.format_report(rep)
    assert rep["counts"]["warn"] == 0 and rep["counts"]["fail"] == 0
    names = _by_name(rep)
    for n in ("python", "config", "data_dir", "workspace", "runner", "docker", "toggles", "tokens"):
        assert n in names
    assert rep["toggles"]["network"] == "off" and rep["toggles"]["host_shell"] == "off"
    json.dumps(rep)  # JSON-serialisable


def test_doctor_is_read_only(tmp_path):
    s = _settings(tmp_path, data_dir=tmp_path / "a" / "b" / "data", workspace=tmp_path / "x" / "ws")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    rep = d.run_doctor(s, env=_safe_env(), which=_nodocker, sockets=())
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    assert {c["status"] for c in _by_name(rep)["data_dir"]} == {"ok"}
    assert "will be created" in _by_name(rep)["workspace"][0]["detail"]


def test_secrets_never_appear(tmp_path):
    env = _safe_env(AVRONE_ADMIN_TOKEN=SECRET, AVRONE_FLEET_TOKEN=SECRET + "x", AVRONE_LLM_API_KEY=SECRET,
                    XAI_API_KEY=SECRET, AVRONE_HOST="0.0.0.0",
                    AVRONE_FLEET_PARENT=f"http://user:{SECRET}@10.0.0.5:8710", DOCKER_HOST=f"tcp://u:{SECRET}@h:2376")
    s = _settings(tmp_path, llm_api_key=SECRET, llm_mode="live",
                  llm_base_url=f"http://bob:{SECRET}@10.1.1.1/v1")
    rep = d.run_doctor(s, env=env, which=_docker, sockets=())
    blob = json.dumps(rep) + d.format_report(rep)
    assert SECRET not in blob
    assert rep["tokens"]["AVRONE_ADMIN_TOKEN"] == "set"
    assert rep["tokens"]["OPENAI_API_KEY"] == "unset"


def test_token_state():
    assert d.token_state(None) == "unset"
    assert d.token_state("") == "unset"
    assert d.token_state("CHANGE-ME") == "placeholder"
    assert d.token_state("short") == "weak"
    assert d.token_state("x" * 16) == "set"


def test_redact_url():
    assert d.redact_url("https://a:b@host/x") == "https://***@host/x"
    assert d.redact_url("") == ""


# ------------------------------------------------------------------ python

@pytest.mark.parametrize("ver,status", [((3, 10, 9), "fail"), ((3, 11, 4), "warn"), ((3, 12, 0), "ok"),
                                        ((3, 13, 1), "ok")])
def test_python_versions(ver, status):
    assert d.check_python(ver).status == status


def test_python_default_is_running_interpreter():
    assert d.check_python().status in ("ok", "warn")


# ------------------------------------------------------------------ paths

def test_path_not_a_directory(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    assert d.check_writable("data_dir", f).status == "fail"


def test_path_not_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(d.os, "access", lambda *a, **k: False)
    assert d.check_writable("data_dir", tmp_path).status == "fail"
    c = d.check_writable("workspace", tmp_path / "missing" / "deeper")
    assert c.status == "fail" and "not writable" in c.detail


def test_path_writable(tmp_path):
    assert d.check_writable("data_dir", tmp_path).status == "ok"


# ------------------------------------------------------------------ runner / docker

def test_bad_runner_fails(tmp_path):
    rep = d.run_doctor(_settings(tmp_path, runner="podman"), env=_safe_env(), which=_docker, sockets=())
    assert not rep["ok"]
    assert _by_name(rep)["runner"][0]["status"] == "fail"
    # unknown runner falls back to local: treated as unsandboxed
    assert rep["toggles"]["host_shell"] == "on"


def test_docker_runner_without_cli_fails(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(), which=_nodocker, sockets=())
    assert _by_name(rep)["docker"][0]["status"] == "fail" and not rep["ok"]


def test_docker_runner_without_daemon_warns(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(), which=_docker, sockets=(str(tmp_path / "nope"),))
    assert _by_name(rep)["docker"][0]["status"] == "warn"


def test_docker_host_env_counts_as_daemon(tmp_path):
    c = d.check_docker("docker", _docker, {"DOCKER_HOST": "unix:///x.sock"}, sockets=())
    assert c[0].status == "ok" and "DOCKER_HOST" in c[0].detail


def test_docker_socket_not_accessible(tmp_path, monkeypatch):
    socks = _sock(tmp_path)
    monkeypatch.setattr(d.os, "access", lambda *a, **k: False)
    c = d.check_docker("docker", _docker, {}, sockets=socks)
    assert [x.status for x in c] == ["ok", "warn"]


def test_auto_and_local_runner_info(tmp_path):
    assert "pick docker" in d.check_docker("auto", _docker, {}, sockets=())[0].detail
    assert "pick local" in d.check_docker("auto", _nodocker, {}, sockets=())[0].detail
    assert d.check_docker("local", _docker, {})[0].status == "info"


# ------------------------------------------------------------------ insecure combos

def test_exposed_without_admin_token_warns(tmp_path):
    rep = d.run_doctor(_settings(tmp_path, agents_can_shell=False), env=_safe_env(AVRONE_HOST="0.0.0.0"),
                       which=_docker, sockets=_sock(tmp_path))
    names = _by_name(rep)
    assert names["security.admin"][0]["status"] == "warn"
    assert "security.exposed_shell" not in names


def test_exposed_shell_warns(tmp_path):
    # v0.27: only AVRONE_PUBLIC_ACCESS=all leaves /api/chat open on an exposed bind
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_HOST="::", AVRONE_ADMIN_TOKEN="a" * 20,
                                                          AVRONE_PUBLIC_ACCESS="all"),
                       which=_docker, sockets=_sock(tmp_path))
    assert "security.exposed_shell" in _by_name(rep)
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_HOST="::", AVRONE_ADMIN_TOKEN="a" * 20),
                       which=_docker, sockets=_sock(tmp_path))
    assert "security.exposed_shell" not in _by_name(rep)


def test_public_access_checks(tmp_path):
    s = _settings(tmp_path)
    s.agents_can_shell = False
    rep = d.run_doctor(s, env=_safe_env(AVRONE_HOST="0.0.0.0", AVRONE_PUBLIC_ACCESS="all"),
                       which=_docker, sockets=_sock(tmp_path))
    assert _by_name(rep)["security.public_access"][0]["status"] == "warn"
    rep = d.run_doctor(s, env=_safe_env(AVRONE_HOST="0.0.0.0", AVRONE_PUBLIC_ACCESS="bogus"),
                       which=_docker, sockets=_sock(tmp_path))
    assert _by_name(rep)["security.public_access"][0]["status"] == "info"
    rep = d.run_doctor(s, env=_safe_env(AVRONE_HOST="0.0.0.0", AVRONE_PUBLIC_ACCESS="admin"),
                       which=_docker, sockets=_sock(tmp_path))
    assert "security.public_access" not in _by_name(rep)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_not_exposed(tmp_path, host):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_HOST=host), which=_docker, sockets=_sock(tmp_path))
    assert "security.admin" not in _by_name(rep) and "security.exposed_shell" not in _by_name(rep)


def test_placeholder_admin_token_fails_and_weak_warns(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_ADMIN_TOKEN="changeme"), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["security.admin_token"][0]["status"] == "fail" and not rep["ok"]
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_ADMIN_TOKEN="abc123"), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["security.admin_token"][0]["status"] == "warn" and rep["ok"]


def test_privileged_fleet_with_open_allowlist(tmp_path):
    env = _safe_env(AVRONE_FLEET_TOKEN="f" * 32)
    rep = d.run_doctor(_settings(tmp_path, runner="local"), env=env, which=_nodocker, sockets=())
    names = _by_name(rep)
    assert names["security.fleet"][0]["status"] == "warn"
    assert names["security.sandbox"][0]["status"] == "warn"
    assert rep["toggles"]["fleet"] == "on" and rep["toggles"]["fleet_allowlist"] == "(any node)"
    # pinning the allowlist clears the fleet warning
    env["AVRONE_FLEET_ALLOW"] = "edge1,edge2"
    rep = d.run_doctor(_settings(tmp_path, runner="local"), env=env, which=_nodocker, sockets=())
    assert "security.fleet" not in _by_name(rep)


def test_sandboxed_fleet_open_allowlist_is_info(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_FLEET_TOKEN="f" * 32), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["security.fleet"][0]["status"] == "info"


def test_weak_fleet_token_and_reuse(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_FLEET_TOKEN="CHANGE-ME"), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["security.fleet_token"][0]["status"] == "fail"
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_FLEET_TOKEN="short"), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["security.fleet_token"][0]["status"] == "warn"
    tok = "z" * 40
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_FLEET_TOKEN=tok, AVRONE_ADMIN_TOKEN=tok),
                       which=_docker, sockets=_sock(tmp_path))
    assert "security.token_reuse" in _by_name(rep)


def test_fleet_cleartext_transport(tmp_path):
    env = _safe_env(AVRONE_FLEET_TOKEN="f" * 32, AVRONE_FLEET_ALLOW="a",
                    AVRONE_FLEET_PARENT="http://10.0.0.2:8710", AVRONE_PUBLIC_URL="https://me.example")
    rep = d.run_doctor(_settings(tmp_path), env=env, which=_docker, sockets=_sock(tmp_path))
    assert len(_by_name(rep)["security.fleet_transport"]) == 1
    env["AVRONE_FLEET_PARENT"] = "http://127.0.0.1:8710"
    rep = d.run_doctor(_settings(tmp_path), env=env, which=_docker, sockets=_sock(tmp_path))
    assert "security.fleet_transport" not in _by_name(rep)


def test_network_on_in_sandbox_is_info(tmp_path):
    rep = d.run_doctor(_settings(tmp_path, allow_network=True), env=_safe_env(), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["security.network"][0]["status"] == "info"


def test_recon_open_allowlist_warns_and_allowlist_clears(tmp_path):
    s = _settings(tmp_path, allow_network=True)
    env = _safe_env(AVRONE_RECON="allow")
    assert "security.recon" in _by_name(d.run_doctor(s, env=env, which=_docker, sockets=_sock(tmp_path)))
    env["AVRONE_RECON_ALLOWLIST"] = "example.com"
    rep = d.run_doctor(s, env=env, which=_docker, sockets=_sock(tmp_path))
    assert "security.recon" not in _by_name(rep) and rep["toggles"]["recon_allowlist"] == "1 entry"


def test_unknown_recon_value_shown_as_fail_closed(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_RECON="maybe"), which=_docker,
                       sockets=_sock(tmp_path))
    assert "fails closed" in rep["toggles"]["recon"]
    assert "security.recon" not in _by_name(rep)


def test_self_modify_on_exposed_node(tmp_path):
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_HOST="0.0.0.0", AVRONE_ADMIN_TOKEN="a" * 20),
                       which=_docker, sockets=_sock(tmp_path))
    assert "security.self_modify" in _by_name(rep)
    rep = d.run_doctor(_settings(tmp_path), env=_safe_env(AVRONE_HOST="0.0.0.0", AVRONE_ADMIN_TOKEN="a" * 20,
                                                          AVRONE_SELF_EVOLVE="deny"),
                       which=_docker, sockets=_sock(tmp_path))
    assert "security.self_modify" not in _by_name(rep)


def test_llm_key_and_cleartext(tmp_path):
    rep = d.run_doctor(_settings(tmp_path, llm_mode="live", provider="xai"), env=_safe_env(), which=_docker,
                       sockets=_sock(tmp_path))
    assert _by_name(rep)["llm.key"][0]["status"] == "warn"
    rep = d.run_doctor(_settings(tmp_path, llm_mode="live", provider="custom", llm_api_key="k" * 20,
                                 llm_base_url="http://10.2.3.4/v1"), env=_safe_env(), which=_docker,
                       sockets=_sock(tmp_path))
    assert "llm.transport" in _by_name(rep) and "llm.key" not in _by_name(rep)
    rep = d.run_doctor(_settings(tmp_path, llm_mode="live", llm_api_key="k" * 20,
                                 llm_base_url="http://host.docker.internal:18789/v1"), env=_safe_env(),
                       which=_docker, sockets=_sock(tmp_path))
    assert "llm.transport" not in _by_name(rep)


def test_strict_turns_warnings_into_not_ok(tmp_path):
    s = _settings(tmp_path, runner="local")
    assert d.run_doctor(s, env=_safe_env(), which=_nodocker, sockets=())["ok"]
    rep = d.run_doctor(s, env=_safe_env(), which=_nodocker, sockets=(), strict=True)
    assert rep["status"] == "warn" and not rep["ok"]
    assert "[strict]" in d.format_report(rep)


# ------------------------------------------------------------------ real Settings + CLI

def test_bad_config_reported_not_raised(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "nonsense")
    rep = d.run_doctor(env={})
    assert not rep["ok"] and _by_name(rep)["config"][0]["status"] == "fail"
    assert "nonsense" in d.format_report(rep)


def test_real_settings_load(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "w"))
    rep = d.run_doctor(env=dict(os.environ), which=_nodocker, sockets=())
    assert _by_name(rep)["config"][0]["status"] == "ok"
    assert not (tmp_path / "d").exists() and not (tmp_path / "w").exists()


def test_cli_doctor(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", SECRET)
    out_json = tmp_path / "rep.json"
    rc = cli_main(["doctor", "--json", str(out_json)])
    out = capsys.readouterr().out
    assert rc == 0 and "AVRONE doctor" in out and "result:" in out
    assert SECRET not in out and SECRET not in out_json.read_text()
    assert json.loads(out_json.read_text())["tokens"]["AVRONE_ADMIN_TOKEN"] == "set"
    assert not (tmp_path / "d").exists()  # doctor never runs build_core


def test_cli_doctor_fail_and_strict_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "w"))
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", "changeme")
    assert cli_main(["doctor"]) == 1
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", "short")  # weak -> warn
    assert cli_main(["doctor"]) == 0
    assert cli_main(["doctor", "--strict"]) == 1
    capsys.readouterr()


def test_fleet_privileged_tier_reporting(tmp_path):
    env = _safe_env(AVRONE_FLEET_TOKEN="f" * 32)
    rep = d.run_doctor(_settings(tmp_path), env=env, which=_docker, sockets=_sock(tmp_path))
    assert rep["toggles"]["fleet_privileged"] == "deny" and "security.fleet_privileged" not in _by_name(rep)
    env["AVRONE_FLEET_PRIVILEGED"] = "allow"
    rep = d.run_doctor(_settings(tmp_path), env=env, which=_docker, sockets=_sock(tmp_path))
    assert _by_name(rep)["security.fleet_privileged"][0]["status"] == "info"  # no nodes listed: refuses all
    env["AVRONE_PRIVILEGED_NODES"] = "router1"
    rep = d.run_doctor(_settings(tmp_path), env=env, which=_docker, sockets=_sock(tmp_path))
    assert _by_name(rep)["security.fleet_privileged"][0]["status"] == "warn"  # open fleet allowlist
    env["AVRONE_FLEET_ALLOW"] = "router1"
    rep = d.run_doctor(_settings(tmp_path), env=env, which=_docker, sockets=_sock(tmp_path))
    assert _by_name(rep)["security.fleet_privileged"][0]["status"] == "info"
