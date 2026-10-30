from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.claude_agent_node import (
    ClaudeAgentNodeError,
    ClaudeVMLease,
    _lease_from_guest_environment,
    _parse_guest_request,
    execute_claude_agent_node,
    validate_lease,
)


def lease(tmp_path: Path) -> ClaudeVMLease:
    return ClaudeVMLease(
        lease_id="lease-1",
        worker_id="worker-1",
        workspace=str(tmp_path),
        disposable_vm=True,
        network_egress_denied=True,
    )


def task_kwargs():
    return {
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "objective": "Inspect the repository and propose a focused patch.",
        "source_repository": "owner/repo",
        "source_commit_sha": "a" * 40,
    }


def test_lease_requires_a_real_provider_declared_disposable_guest(tmp_path):
    good = validate_lease(lease(tmp_path))
    assert good == tmp_path.resolve()

    bad = ClaudeVMLease(
        lease_id="lease-1", worker_id="worker-1", workspace=str(tmp_path),
        disposable_vm=False, network_egress_denied=True,
    )
    with pytest.raises(ClaudeAgentNodeError, match="disposable VM"):
        validate_lease(bad)


def test_lease_rejects_network_and_ambient_credentials(tmp_path):
    network = ClaudeVMLease(
        lease_id="lease-1", worker_id="worker-1", workspace=str(tmp_path),
        disposable_vm=True, network_egress_denied=False,
    )
    with pytest.raises(ClaudeAgentNodeError, match="network egress"):
        validate_lease(network)

    credentials = ClaudeVMLease(
        lease_id="lease-1", worker_id="worker-1", workspace=str(tmp_path),
        disposable_vm=True, network_egress_denied=True, github_credentials_mounted=True,
    )
    with pytest.raises(ClaudeAgentNodeError, match="GitHub credentials"):
        validate_lease(credentials)


@pytest.mark.asyncio
async def test_worker_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_CLAUDE_SDK_ENABLED", raising=False)
    with pytest.raises(ClaudeAgentNodeError, match="disabled"):
        await execute_claude_agent_node(lease(tmp_path), **task_kwargs())


@pytest.mark.asyncio
async def test_sdk_execution_uses_bounded_tools_and_reports_worker_receipt_only(
    tmp_path, monkeypatch
):
    captured = {}

    class AgentDefinition:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        is_error = False
        session_id = "session-1"
        result = "Changed one file; tests remain for the trusted runner."
        num_turns = 3
        total_cost_usd = 0.12

    async def fake_query(prompt, *, options):
        captured["prompt"] = prompt
        captured["query_options"] = options
        yield AssistantMessage([TextBlock("Working summary.")])
        yield ResultMessage()

    fake_sdk = SimpleNamespace(
        __version__="0.test",
        AgentDefinition=AgentDefinition,
        ClaudeAgentOptions=ClaudeAgentOptions,
        AssistantMessage=AssistantMessage,
        TextBlock=TextBlock,
        ResultMessage=ResultMessage,
        query=fake_query,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.setenv("AVRONE_CLAUDE_SDK_ENABLED", "1")

    result = await execute_claude_agent_node(lease(tmp_path), **task_kwargs())

    options = captured["options"]
    assert options["tools"] == ["Read", "Write", "Edit", "Glob", "Grep", "Agent"]
    assert "Bash" in options["disallowed_tools"]
    assert options["setting_sources"] == []
    assert options["permission_mode"] == "dontAsk"
    assert options["sandbox"]["network"]["allowedDomains"] == []
    assert options["sandbox"]["network"]["allowAllUnixSockets"] is False
    assert options["max_turns"] == 10
    assert options["max_budget_usd"] == 0.5
    assert "owner/repo@" in captured["prompt"]

    assert result["status"] == "completed"
    assert result["worker_id"] == "worker-1"
    assert result["lease_id"] == "lease-1"
    assert result["session_id"] == "session-1"
    assert result["cost_usd"] == 0.12
    assert "artifact_digests" not in result


@pytest.mark.asyncio
async def test_worker_rejects_unbounded_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CLAUDE_SDK_ENABLED", "1")
    monkeypatch.setenv("AVRONE_CLAUDE_MAX_BUDGET_USD", "100")
    with pytest.raises(ClaudeAgentNodeError, match="budget"):
        await execute_claude_agent_node(lease(tmp_path), **task_kwargs())


def test_guest_request_parser_rejects_extra_fields_and_oversize():
    import json
    valid = {
        "task_id": "task-1",
        "correlation_id": "corr-1",
        "objective": "Inspect source.",
        "source_repository": "owner/repo",
        "source_commit_sha": "a" * 40,
    }
    assert _parse_guest_request(json.dumps(valid).encode()) == valid
    with pytest.raises(ClaudeAgentNodeError, match="shape"):
        _parse_guest_request(json.dumps({**valid, "api_key": "never"}).encode())
    with pytest.raises(ClaudeAgentNodeError, match="24 KiB"):
        _parse_guest_request(b"x" * (24 * 1024 + 1))


def test_guest_lease_environment_requires_explicit_security_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_VM_LEASE_ID", "lease-1")
    monkeypatch.setenv("AVRONE_VM_WORKER_ID", "worker-1")
    monkeypatch.setenv("AVRONE_VM_WORKSPACE", str(tmp_path))
    for name in (
        "AVRONE_VM_DISPOSABLE",
        "AVRONE_VM_NETWORK_EGRESS_DENIED",
        "AVRONE_VM_DOCKER_SOCKET_MOUNTED",
        "AVRONE_VM_GITHUB_CREDENTIALS_MOUNTED",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ClaudeAgentNodeError, match="explicitly set"):
        _lease_from_guest_environment()

    monkeypatch.setenv("AVRONE_VM_DISPOSABLE", "1")
    monkeypatch.setenv("AVRONE_VM_NETWORK_EGRESS_DENIED", "1")
    monkeypatch.setenv("AVRONE_VM_DOCKER_SOCKET_MOUNTED", "0")
    monkeypatch.setenv("AVRONE_VM_GITHUB_CREDENTIALS_MOUNTED", "0")
    assert validate_lease(_lease_from_guest_environment()) == tmp_path.resolve()
