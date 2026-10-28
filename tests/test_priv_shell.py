"""Offline tests for the deny-by-default `priv_shell` tool.

A fake runner records every command it is asked to run; nothing touches a real shell.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import tools_extra as tx
from avrone.sovereign import CommandResult
from avrone.tools import builtin_registry


class FakeRunner:
    kind = "local"
    workspace = "/nonexistent"

    def __init__(self):
        self.calls = []

    async def run(self, cmd, timeout=None):
        self.calls.append((cmd, timeout))
        return CommandResult(cmd, 0, "fake-out\n", "")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AVRONE_PRIV_SHELL_ALLOW", raising=False)
    monkeypatch.delenv("AVRONE_PRIV_SHELL_TIMEOUT", raising=False)


def make_ctx(priv="", grant=None):
    """`priv` = core.fleet_privileged; `grant` = the run-scoped PRIV_GRANT (defaults to the same value,
    as fleet.handle_run sets both after the signed gate accepts)."""
    core = SimpleNamespace(runner=FakeRunner(), codex=MagicMock(), fleet_privileged=priv)
    g = priv if grant is None else grant
    return SimpleNamespace(core=core, name="Ops-1", run_id="r1", depth=0,
                           grant=g if isinstance(g, str) else "")


def in_run(ctx, coro):
    with tx.priv_grant(ctx.grant):
        return run(coro)


def events(ctx, kind):
    return [c for c in ctx.core.codex.emit.call_args_list if c.args[1] == kind]


def shell(ctx, cmd, **kw):
    return in_run(ctx, tx.t_priv_shell(ctx, {"cmd": cmd, **kw}))


# ---------------------------------------------------------------- deny by default
@pytest.mark.parametrize("priv", ["", "priv:admin", "PRIV:SHELL", "priv:shell ", None, True, 1])
def test_refused_without_granted_shell_capability(monkeypatch, priv):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx(priv)
    out = shell(ctx, "ls")
    assert out.startswith("ERROR: requires a granted priv:shell capability")
    assert ctx.core.runner.calls == []
    ev = events(ctx, "priv.refused")
    assert len(ev) == 1 and ev[0].kwargs["code"] == "no_capability" and ev[0].kwargs["cmd"] == "ls"
    assert not events(ctx, "priv.shell")


def test_refused_when_core_has_no_privileged_attribute(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx()
    del ctx.core.fleet_privileged
    assert shell(ctx, "ls").startswith("ERROR: requires a granted priv:shell capability")
    assert ctx.core.runner.calls == []


@pytest.mark.parametrize("priv,grant", [
    ("priv:shell", ""),            # core flag set by some OTHER concurrent privileged run: no run-scoped grant
    ("", "priv:shell"),            # run-scoped grant without the core flag
    ("priv:shell", "priv:admin"),  # the two halves disagree
])
def test_refused_unless_core_flag_and_run_grant_agree(monkeypatch, priv, grant):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx(priv, grant)
    assert shell(ctx, "ls").startswith("ERROR: requires a granted priv:shell capability")
    assert ctx.core.runner.calls == []


def test_empty_allowlist_refuses_everything_even_when_granted():
    ctx = make_ctx("priv:shell")
    out = shell(ctx, "ls")
    assert out.startswith("ERROR:") and "AVRONE_PRIV_SHELL_ALLOW is empty" in out
    assert ctx.core.runner.calls == []
    assert events(ctx, "priv.refused")[0].kwargs["code"] == "allowlist_empty"


@pytest.mark.parametrize("cmd", ["rm -rf /", "bash -c id", "python3 -c 1", "sh", "lsx", "LS"])
def test_refused_for_non_allowlisted_binary(monkeypatch, cmd):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls,cat,git")
    ctx = make_ctx("priv:shell")
    out = shell(ctx, cmd)
    assert out.startswith("ERROR:") and "not in AVRONE_PRIV_SHELL_ALLOW" in out
    assert ctx.core.runner.calls == []
    assert events(ctx, "priv.refused")[0].kwargs["code"] == "not_allowlisted"


@pytest.mark.parametrize("cmd", ["/bin/ls", "./ls", "/tmp/evil/ls -l", "FOO=1 ls", "../ls"])
def test_paths_and_env_prefixes_never_match_the_allowlist(monkeypatch, cmd):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx("priv:shell")
    assert shell(ctx, cmd).startswith("ERROR:")
    assert ctx.core.runner.calls == []


@pytest.mark.parametrize("cmd", [
    "ls; rm -rf /", "ls | sh", "ls && id", "ls || id", "ls & id", "cat `id`", "cat $(id)",
    "cat $HOME", "ls > /etc/passwd", "cat < /etc/shadow", "ls >> x", "ls\nid", "ls\rid",
    "ls 'a;b'", 'cat "$X"',
])
def test_refused_for_metacharacters_and_pipelines(monkeypatch, cmd):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls,cat")
    ctx = make_ctx("priv:shell")
    out = shell(ctx, cmd)
    assert out.startswith("ERROR: shell metacharacters")
    assert ctx.core.runner.calls == []
    assert events(ctx, "priv.refused")[0].kwargs["code"] == "metachar"


@pytest.mark.parametrize("cmd,code", [("", "empty"), ("   ", "empty"), ("ls 'unterminated", "parse"),
                                      ("ls " + "a " * 70, "too_many_args"), ("ls " + "a" * 2100, "too_long")])
def test_refused_for_malformed_commands(monkeypatch, cmd, code):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx("priv:shell")
    assert shell(ctx, cmd).startswith("ERROR:")
    assert ctx.core.runner.calls == []
    assert events(ctx, "priv.refused")[0].kwargs["code"] == code


def test_allowlist_ignores_non_bare_entries(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", " ls , /bin/sh, *, ,git,a b")
    assert tx.priv_shell_allowlist() == {"ls", "git"}


# ---------------------------------------------------------------- the one allowed path
def test_allowed_and_run_only_when_granted_and_allowlisted(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls,cat,git")
    ctx = make_ctx("priv:shell")
    out = shell(ctx, "git log --oneline -3")
    assert "fake-out" in out and "[exit 0]" in out
    assert ctx.core.runner.calls == [("git log --oneline -3", 30)]
    ev = events(ctx, "priv.shell")
    assert len(ev) == 1
    assert ev[0].kwargs["cmd"] == "git log --oneline -3" and ev[0].kwargs["argv0"] == "git"
    assert ev[0].kwargs["exit_code"] == 0 and ev[0].kwargs["timed_out"] is False
    assert not events(ctx, "priv.refused")


def test_arguments_are_requoted_so_globs_do_not_expand(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx("priv:shell")
    shell(ctx, "ls * 'my dir' ~")
    assert ctx.core.runner.calls[0][0] == "ls '*' 'my dir' '~'"


def test_every_attempt_is_audited(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx("priv:shell")
    for c in ("ls", "cat x", "ls;id", "ls -la"):
        shell(ctx, c)
    assert len(events(ctx, "priv.shell")) == 2 and len(events(ctx, "priv.refused")) == 2


def test_runner_failure_is_audited(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    ctx = make_ctx("priv:shell")

    async def boom(cmd, timeout=None):
        raise RuntimeError("runner down")
    ctx.core.runner.run = boom
    assert shell(ctx, "ls") == "ERROR: RuntimeError: runner down"
    assert events(ctx, "priv.shell")[0].kwargs["error"].startswith("RuntimeError")


# ---------------------------------------------------------------- timeout bound
def test_timeout_is_bounded_by_ceiling(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    monkeypatch.setenv("AVRONE_PRIV_SHELL_TIMEOUT", "10")
    ctx = make_ctx("priv:shell")
    shell(ctx, "ls", timeout=9999)
    shell(ctx, "ls", timeout=3)
    shell(ctx, "ls", timeout=-5)
    shell(ctx, "ls")
    assert [t for _, t in ctx.core.runner.calls] == [10, 3, 1, 10]


@pytest.mark.parametrize("val,expect", [("", 30), ("junk", 30), ("0", 1), ("99999", 300), ("45", 45)])
def test_timeout_env_is_clamped(monkeypatch, val, expect):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_TIMEOUT", val)
    assert tx.priv_shell_timeout() == expect


# ---------------------------------------------------------------- registry
def test_registered_and_refuses_through_registry(monkeypatch):
    monkeypatch.setenv("AVRONE_PRIV_SHELL_ALLOW", "ls")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    assert "priv_shell" in reg.names()
    ctx = make_ctx("")
    out = in_run(ctx, reg.call(ctx, "priv_shell", {"cmd": "ls"}))
    assert out.startswith("ERROR: requires a granted priv:shell capability")
    assert ctx.core.runner.calls == []
