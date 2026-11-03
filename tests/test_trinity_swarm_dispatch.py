"""Trinity routes through real durable state without invoking local model workers."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from avrone import server, swarm_runtime
from avrone.checkpoint import Checkpoint
from avrone.swarm_client import DelegationUnknownOutcome
from avrone.trinity import TrinityCore, squad_dispatch_mode
from tests.test_swarm_api import FakeAuthority, FakeClient


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, run_id, kind, who, **payload):
        self.rows.append({"run_id": run_id, "kind": kind, "who": who, **payload})


def make_core(tmp_path, monkeypatch, authority=None):
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=2, deep_squad_size=2, data_dir=tmp_path)
    core.codex = Events()
    core._privileged_run = lambda: False
    client = FakeClient()
    authority = authority or FakeAuthority()
    monkeypatch.setattr(swarm_runtime.SwarmClient, "from_env", lambda: client)
    monkeypatch.setattr(swarm_runtime.CapabilityAuthorityClient, "from_env", lambda: authority)
    monkeypatch.setattr(swarm_runtime, "load_parent_capability_file", lambda: ({"id": "parent"}, "sig"))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Swarm dispatch must not invoke a local planner or worker")

    core.plan = forbidden
    core.run_agent = forbidden
    return core, client, authority


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_swarm_routes_whole_objective_without_local_model_calls(tmp_path, monkeypatch, explicit):
    if not explicit:
        monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "swarm")
    core, client, authority = make_core(tmp_path, monkeypatch)
    result = await core.squad("approved objective", run_id="run-1", dispatch="swarm" if explicit else None,
                              swarm_correlation_id="stable-correlation")
    assert result["status"] == "queued" and result["agents"] == []
    assert result["swarm"]["correlation_id"] == "stable-correlation"
    assert len(client.calls) == len(authority.calls) == 1
    assert {e["kind"] for e in core.codex.rows} == {"run.start", "swarm.dispatch", "run.queued"}
    assert "completion is unconfirmed" in result["answer"]


@pytest.mark.asyncio
async def test_timeout_restarts_with_persisted_assertion_and_stable_ids(tmp_path, monkeypatch):
    authority = FakeAuthority([DelegationUnknownOutcome("timeout", {"correlation_id": "stable"}), None])
    core, client, _ = make_core(tmp_path, monkeypatch, authority)
    first = await core.squad("objective", run_id="run-first", dispatch="swarm", swarm_correlation_id="stable")
    assert first["status"] == "pending" and not client.calls
    original = dict(authority.calls[0])

    def forbidden():
        raise AssertionError("must resume the persisted assertion without reading the parent")

    next_core, next_client, _ = make_core(tmp_path, monkeypatch, authority)
    monkeypatch.setattr(swarm_runtime, "load_parent_capability_file", forbidden)
    authority._assertion = forbidden
    second = await next_core.squad("objective", run_id="run-after-restart", dispatch="swarm",
                                    swarm_correlation_id="stable")
    assert second["status"] == "queued" and len(next_client.calls) == 1
    assert authority.calls[1] == {**original, "allow_expired_parent_replay": True}
    assert first["swarm"]["message_id"] == second["swarm"]["message_id"]


@pytest.mark.asyncio
async def test_swarm_ignores_cached_local_answer_and_keeps_checkpoint_identity(tmp_path, monkeypatch):
    core, client, _ = make_core(tmp_path / "data", monkeypatch)
    path = tmp_path / "checkpoint.json"
    ck = Checkpoint(path)
    ck.begin("objective", "squad", "original-run", n=2)
    ck.put("result", {"answer": "old local answer"})
    result = await core.squad("objective", checkpoint=path, dispatch="swarm")
    assert result["status"] == "queued" and result["run_id"] == "original-run"
    assert len(client.calls) == 1
    monkeypatch.setattr(swarm_runtime.CapabilityAuthorityClient, "from_env", lambda: (_ for _ in ()).throw(
        AssertionError("queued result must not redelegate")))
    again = await core.squad("objective", checkpoint=path, dispatch="swarm")
    assert again["status"] == "queued" and again["run_id"] == "original-run"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_privileged_context_is_refused_before_opening_dispatcher(tmp_path, monkeypatch):
    core, _, _ = make_core(tmp_path, monkeypatch)
    core._privileged_run = lambda: True
    monkeypatch.setattr(swarm_runtime, "open_dispatcher", lambda *a: (_ for _ in ()).throw(
        AssertionError("privileged task must not touch signing configuration")))
    result = await core.squad("sensitive", dispatch="swarm")
    assert result["status"] == "rejected"
    assert result["swarm"]["reason"] == "privileged_run_cannot_be_exported"


@pytest.mark.asyncio
async def test_missing_trust_configuration_returns_bounded_reason(tmp_path, monkeypatch):
    core, _, _ = make_core(tmp_path, monkeypatch)
    monkeypatch.setattr(swarm_runtime.SwarmClient, "from_env", lambda: (_ for _ in ()).throw(
        RuntimeError("private-path-and-secret-must-not-escape")))
    result = await core.squad("objective", dispatch="swarm")
    assert result["status"] == "disabled" and result["agents"] == []
    assert "private-path" not in str(result)
    assert "run.done" not in {e["kind"] for e in core.codex.rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("correlation", ["../bad", "bad\n", "a" * 129, "", 0])
async def test_invalid_correlation_is_refused_without_network(tmp_path, monkeypatch, correlation):
    core, client, authority = make_core(tmp_path, monkeypatch)
    result = await core.squad("objective", dispatch="swarm", swarm_correlation_id=correlation)
    assert result["status"] == "rejected"
    assert not client.calls and not authority.calls


@pytest.mark.asyncio
async def test_checkpoint_size_change_keeps_one_queued_objective(tmp_path, monkeypatch):
    core, client, authority = make_core(tmp_path / "data", monkeypatch)
    checkpoint = tmp_path / "run.json"
    first = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm")
    core.settings.squad_size = 8
    second = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm")
    assert first["swarm"]["message_id"] == second["swarm"]["message_id"]
    assert len(client.calls) == len(authority.calls) == 1


@pytest.mark.asyncio
async def test_checkpoint_remembers_explicit_correlation_without_repeating_argument(tmp_path, monkeypatch):
    core, client, authority = make_core(tmp_path / "data", monkeypatch)
    checkpoint = tmp_path / "run.json"
    first = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm", swarm_correlation_id="chosen-id")
    second = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm")
    assert first["swarm"]["message_id"] == second["swarm"]["message_id"]
    assert len(client.calls) == len(authority.calls) == 1
    rejected = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm", swarm_correlation_id="new-id")
    assert rejected["status"] == "rejected"
    assert len(client.calls) == len(authority.calls) == 1


@pytest.mark.asyncio
async def test_corrupt_or_deleted_local_checkpoint_cannot_remint_queue_identity(tmp_path, monkeypatch):
    core, client, authority = make_core(tmp_path / "data", monkeypatch)
    checkpoint = tmp_path / "run.json"
    first = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm", swarm_correlation_id="chosen-id")
    checkpoint.write_text("{broken JSON")
    second = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm")
    checkpoint.unlink()
    third = await core.squad("objective", checkpoint=checkpoint, dispatch="swarm")
    assert first["swarm"]["message_id"] == second["swarm"]["message_id"] == third["swarm"]["message_id"]
    assert len(client.calls) == len(authority.calls) == 1


@pytest.mark.asyncio
async def test_atomic_replacement_of_checkpoint_leaf_symlink_keeps_queue_identity(tmp_path, monkeypatch):
    core, client, authority = make_core(tmp_path / "data", monkeypatch)
    target = tmp_path / "original.json"
    Checkpoint(target).begin("objective", "squad", "original-run", n=2)
    alias = tmp_path / "checkpoint.json"
    alias.symlink_to(target)
    first = await core.squad("objective", checkpoint=alias, dispatch="swarm")
    assert alias.is_symlink()
    core.settings.squad_size = 7
    second = await core.squad("objective", checkpoint=alias, dispatch="swarm")
    assert not alias.is_symlink()
    assert first["swarm"]["message_id"] == second["swarm"]["message_id"]
    assert len(client.calls) == len(authority.calls) == 1


@pytest.mark.asyncio
async def test_deep_cannot_treat_queue_acceptance_as_first_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "swarm")
    core, client, authority = make_core(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="first_answer"):
        await core.deep("objective")
    assert not client.calls and not authority.calls


def test_unknown_environment_dispatch_retains_local_default(monkeypatch):
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "bogus")
    assert squad_dispatch_mode() == "local"
    with pytest.raises(ValueError, match="dispatch must"):
        squad_dispatch_mode("bogus")


@pytest.mark.parametrize("dispatch", ["swarm", None])
@pytest.mark.parametrize("policy", ["all", "loopback", "admin"])
def test_chat_swarm_requires_operator_even_on_public_chat(tmp_path, monkeypatch, dispatch, policy):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", policy)
    if dispatch is None:
        monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "swarm")
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    body = {"message": "objective", "mode": "squad"}
    if dispatch:
        body["dispatch"] = dispatch
    with TestClient(server.app) as http:
        assert http.post("/api/chat", json=body).status_code in (401, 403)
        assert http.post("/api/chat", json=body, headers={"X-Admin-Token": "wrong"}).status_code in (401, 403)


def test_authorized_chat_passes_correlation_and_refuses_incompatible_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    calls = []

    async def squad(task, n, run_id, **kwargs):
        calls.append(kwargs)
        return {"run_id": run_id, "status": "queued", "agents": []}

    with TestClient(server.app) as http:
        monkeypatch.setattr(server.core(), "squad", squad)
        headers = {"X-Admin-Token": "test-token"}
        body = {"message": "objective", "mode": "squad", "dispatch": "swarm",
                "swarm_correlation_id": "operator-stable-id"}
        assert http.post("/api/chat", json=body, headers=headers).json()["status"] == "queued"
        assert calls == [{"dispatch": "swarm", "swarm_correlation_id": "operator-stable-id"}]
        for updates in ({"mode": "deep"}, {"dispatch": "local"}, {"mode": "chat"}):
            assert http.post("/api/chat", json={**body, **updates}, headers=headers).status_code == 400
        assert http.post("/api/chat", json={**body, "swarm_correlation_id": "../bad"},
                         headers=headers).status_code == 422
        for message in (" ", "x" * 2001):
            assert http.post("/api/chat", json={**body, "message": message}, headers=headers).status_code == 422
        assert len(calls) == 1
