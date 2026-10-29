"""Edge cases for persistent per-agent container lifecycle and resource bounds."""
import asyncio
from types import SimpleNamespace

import pytest

from avrone import agentcontainer


def _result(code=0, stdout="", stderr=""):
    return SimpleNamespace(exit_code=code, stdout=stdout, stderr=stderr)


def _value(args, option):
    return args[args.index(option) + 1]


def test_explicit_container_resource_overrides_are_clamped():
    args = agentcontainer.hardening_args(False, mem=10**9, cpu=10**6)
    assert _value(args, "--memory") == f"{agentcontainer.MAX_MEM_MB}m"
    assert float(_value(args, "--cpus")) == agentcontainer.MAX_CPUS
    assert _value(args, "--pids-limit") == str(agentcontainer.PIDS_LIMIT)
    assert _value(args, "--network") == "none"
    assert _value(args, "--cap-drop") == "ALL"


@pytest.mark.parametrize("mem,cpu", [(-1, 0), (float("nan"), float("inf")), ("junk", "bad")])
def test_invalid_explicit_resource_overrides_fall_back_to_safe_defaults(mem, cpu):
    args = agentcontainer.hardening_args(True, mem=mem, cpu=cpu)
    assert _value(args, "--memory") == f"{agentcontainer.DEFAULT_MEM_MB}m"
    assert float(_value(args, "--cpus")) == agentcontainer.DEFAULT_CPUS
    assert "--network" not in args


def test_unknown_container_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "root")
    assert agentcontainer.container_mode() == "off"
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    assert agentcontainer.container_mode() == "persistent"


def test_concurrent_ensure_creates_only_one_home_container(tmp_path):
    calls = []

    async def execute(argv, timeout):
        calls.append((list(argv), timeout))
        action = argv[1]
        if action == "inspect":
            return _result(1, "", "not found")
        if action == "rm":
            return _result(0, "")
        if action == "run":
            await asyncio.sleep(0.01)
            return _result(0, "container-id")
        raise AssertionError(f"unexpected Docker action: {action}")

    home = tmp_path / "workspace" / "agents" / "researcher"
    home.mkdir(parents=True)
    pool = agentcontainer.AgentContainerPool(execute, "python:3.12-slim", False,
                                             node="node-a", idle=30)

    async def exercise():
        return await asyncio.gather(pool.ensure(home), pool.ensure(home), pool.ensure(home))

    names = asyncio.run(exercise())
    assert len(set(names)) == 1
    assert sum(argv[1] == "run" for argv, _ in calls) == 1
    assert pool.homes[names[0]].uses == 3


def test_idle_reaper_respects_skip_and_exact_idle_boundary(tmp_path):
    tick = [0.0]
    calls = []

    async def execute(argv, timeout):
        calls.append(list(argv))
        if argv[1] == "inspect":
            return _result(1, "", "not found")
        if argv[1] == "rm":
            return _result(0, "")
        if argv[1] == "run":
            return _result(0, "container-id")
        raise AssertionError(argv)

    a = tmp_path / "workspace" / "agents" / "alpha"
    b = tmp_path / "workspace" / "agents" / "beta"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    pool = agentcontainer.AgentContainerPool(execute, "python:3.12-slim", False,
                                             node="node-a", clock=lambda: tick[0], idle=5)

    async def exercise():
        alpha = await pool.ensure(a)
        beta = await pool.ensure(b)
        tick[0] = 5.0
        removed = await pool.reap_idle(skip=alpha)
        return alpha, beta, removed

    alpha, beta, removed = asyncio.run(exercise())
    assert removed == [beta]
    assert alpha in pool.homes and beta not in pool.homes
    assert pool.reaped == 1
    assert any(argv[1] == "rm" and argv[-1] == beta for argv in calls)


def test_failed_container_start_leaves_no_phantom_home(tmp_path):
    async def execute(argv, timeout):
        if argv[1] == "inspect":
            return _result(1, "", "not found")
        if argv[1] == "rm":
            return _result(1, "", "not found")
        if argv[1] == "run":
            return _result(125, "", "daemon unavailable")
        raise AssertionError(argv)

    home = tmp_path / "workspace" / "agents" / "failed"
    home.mkdir(parents=True)
    pool = agentcontainer.AgentContainerPool(execute, "python:3.12-slim", False, node="node-a")
    with pytest.raises(RuntimeError, match="could not start agent container"):
        asyncio.run(pool.ensure(home))
    assert pool.homes == {}
    assert pool.created == 0
