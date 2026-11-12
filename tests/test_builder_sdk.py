from __future__ import annotations

import pytest

from avrone.builder_sdk import BuilderAgentSDK, BuilderPolicy, BuildResult
from avrone import agentcontainer


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []

    async def run(self, command, timeout=None):
        self.commands.append(command)
        return self.outputs.pop(0)


class FakeCommand:
    def __init__(self, exit_code=0, stdout="", stderr="", timed_out=False):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class FakeCore:
    def __init__(self, raw=None, runner=None):
        self.raw = raw or {"run_id": "r1", "answer": "Implemented draft."}
        self.runner = runner or FakeRunner([])
        self.calls = []

    async def squad(self, task, **kwargs):
        self.calls.append(("squad", task, kwargs))
        return self.raw

    async def chat(self, task, **kwargs):
        self.calls.append(("chat", task, kwargs))
        return self.raw

    async def deep(self, task, **kwargs):
        self.calls.append(("deep", task, kwargs))
        return self.raw


@pytest.mark.asyncio
async def test_build_uses_existing_trinity_squad_and_marks_unverified():
    core = FakeCore()
    sdk = BuilderAgentSDK(core, policy=BuilderPolicy())
    result = await sdk.build("Build a vertical slice", workers=3, run_id="build-1")
    assert result.run_id == "r1"
    assert result.status == "UNVERIFIED"
    assert core.calls[0][0] == "squad"
    assert "AVRONE Control Room" in core.calls[0][1]
    assert core.calls[0][2]["n"] == 3


@pytest.mark.asyncio
async def test_build_rejects_invalid_mode_and_oversized_task():
    sdk = BuilderAgentSDK(FakeCore(), policy=BuilderPolicy(max_task_chars=8))
    with pytest.raises(ValueError):
        await sdk.build("123456789")
    with pytest.raises(ValueError):
        await sdk.build("ok", mode="shell")


@pytest.mark.asyncio
async def test_verify_pass_without_sha_remains_unverified():
    runner = FakeRunner([
        FakeCommand(exit_code=128, stderr="not a git repository"),
        FakeCommand(exit_code=0),
    ])
    sdk = BuilderAgentSDK(
        FakeCore(runner=runner),
        policy=BuilderPolicy(verification_commands=("python -m pytest -q",)),
    )
    result = BuildResult(run_id="r", mode="squad", status="UNVERIFIED", answer="draft")
    evidence = await sdk.verify(result)
    assert evidence.status == "PASS"
    assert result.status == "UNVERIFIED"
    assert evidence.target_sha is None


@pytest.mark.asyncio
async def test_verify_failure_is_not_hidden():
    runner = FakeRunner([
        FakeCommand(exit_code=0, stdout="a" * 40),
        FakeCommand(exit_code=1, stderr="test failure"),
    ])
    sdk = BuilderAgentSDK(
        FakeCore(runner=runner),
        policy=BuilderPolicy(verification_commands=("python -m pytest -q",)),
    )
    result = BuildResult(run_id="r", mode="squad", status="UNVERIFIED", answer="draft")
    evidence = await sdk.verify(result)
    assert evidence.status == "FAIL"
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_verify_rejects_model_supplied_command():
    sdk = BuilderAgentSDK(
        FakeCore(runner=FakeRunner([])),
        policy=BuilderPolicy(verification_commands=("python -m pytest -q",)),
    )
    result = BuildResult(run_id="r", mode="squad", status="UNVERIFIED", answer="draft")
    with pytest.raises(ValueError):
        await sdk.verify(result, commands=("rm -rf /",))


@pytest.mark.asyncio
async def test_close_is_idempotent():
    calls = []

    async def cleanup():
        calls.append("closed")

    sdk = BuilderAgentSDK(FakeCore(), cleanups=[cleanup])
    await sdk.close()
    await sdk.close()
    assert calls == ["closed"]


@pytest.mark.asyncio
async def test_verify_with_sha_and_passing_checks_is_verified():
    runner = FakeRunner([
        FakeCommand(exit_code=0, stdout="b" * 40),
        FakeCommand(exit_code=0),
    ])
    sdk = BuilderAgentSDK(
        FakeCore(runner=runner),
        policy=BuilderPolicy(verification_commands=("python -m pytest -q",)),
    )
    result = BuildResult(run_id="r", mode="squad", status="UNVERIFIED", answer="draft")
    evidence = await sdk.verify(result)
    assert evidence.status == "PASS"
    assert evidence.target_sha == "b" * 40
    assert result.status == "VERIFIED"


@pytest.mark.asyncio
async def test_verify_refuses_to_run_on_wrong_expected_sha():
    runner = FakeRunner([FakeCommand(exit_code=0, stdout="a" * 40)])
    sdk = BuilderAgentSDK(
        FakeCore(runner=runner),
        policy=BuilderPolicy(
            verification_commands=("python -m pytest -q",),
            expected_sha="b" * 40,
        ),
    )
    result = BuildResult(run_id="r", mode="squad", status="UNVERIFIED", answer="draft")
    evidence = await sdk.verify(result)
    assert evidence.status == "FAIL"
    assert "SHA mismatch" in evidence.reason
    assert evidence.commands == []
    assert runner.commands == ["git rev-parse --verify HEAD"]
    assert result.status == "FAIL"


def test_agent_container_hardening_is_non_root_readonly_and_networkless(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    args = agentcontainer.hardening_args(False, mem=512, cpu=1.0)
    assert args[args.index("--user") + 1] == "501:20"
    assert "--read-only" in args
    assert args[args.index("--tmpfs") + 1] == "/tmp:rw,nosuid,nodev,size=128m"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--memory-swap") + 1] == "512m"
    assert args[args.index("--cpus") + 1] == "1.0"
    assert args[args.index("--network") + 1] == "none"


def test_agent_container_rejects_root_identity(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "0")
    monkeypatch.setenv("AVRONE_AGENT_GID", "0")
    with pytest.raises(RuntimeError, match="positive non-root"):
        agentcontainer.hardening_args(False)


def test_persistent_agent_container_mounts_only_agent_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    home = tmp_path / "workspace" / "agents" / "reviewer"
    home.mkdir(parents=True)
    argv = agentcontainer.persistent_run_argv(
        "avrone-agent-node-reviewer", home, "python:3.12-slim",
        allow_network=False, agent_id="reviewer",
    )
    assert argv.count("-v") == 1
    assert argv[argv.index("-v") + 1] == f"{home.resolve()}:/agent"
    assert argv[argv.index("-w") + 1] == "/agent"
    assert argv[argv.index("--label") + 1] == "avrone.agent-home=1"
    assert argv[-3:] == ["python:3.12-slim", "sleep", "infinity"]
