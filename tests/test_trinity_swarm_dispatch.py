import asyncio
import os
from types import SimpleNamespace

import pytest

from avrone.trinity import TrinityCore


class FakeCodex:
    def __init__(self):
        self.events = []
    def emit(self, *args, **kwargs):
        self.events.append((args, kwargs))


@pytest.mark.asyncio
async def test_explicit_swarm_dispatch_bypasses_local_planner_and_workers(monkeypatch):
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=3)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    calls = []
    async def dispatch(task, run_id, correlation_id):
        calls.append((task, run_id, correlation_id))
        return {"run_id": run_id, "status": "queued", "answer": "queued", "agents": []}
    async def forbidden(*args, **kwargs):
        raise AssertionError("Swarm mode must not enter local planning or worker execution")
    core._dispatch_swarm = dispatch
    core.plan = forbidden
    core.run_agent = forbidden
    result = await core.squad("objective", run_id="run-1", dispatch="swarm",
                              swarm_correlation_id="stable-correlation")
    assert result["status"] == "queued"
    assert calls == [("objective", "run-1", "stable-correlation")]


@pytest.mark.asyncio
async def test_environment_swarm_dispatch_bypasses_local_path(monkeypatch):
    monkeypatch.setenv("AVRONE_SQUAD_DISPATCH", "swarm")
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=2)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], {"status": "stale"})
    calls = []
    async def dispatch(task, run_id, correlation_id):
        calls.append((task, run_id, correlation_id))
        return {"run_id": run_id, "status": "pending", "answer": "pending", "agents": []}
    async def forbidden(*args, **kwargs):
        raise AssertionError("cached checkpoint/local path must not override Swarm outbox")
    core._dispatch_swarm = dispatch
    core.plan = forbidden
    core.run_agent = forbidden
    result = await core.squad("objective", run_id="run-2")
    assert result["status"] == "pending"
    assert calls == [("objective", "run-2", None)]


@pytest.mark.asyncio
async def test_unknown_dispatch_value_is_rejected():
    core = TrinityCore.__new__(TrinityCore)
    core.settings = SimpleNamespace(squad_size=1)
    core.codex = FakeCodex()
    core._ck_open = lambda *args, **kwargs: (None, args[3], None)
    with pytest.raises(ValueError, match="dispatch must be"):
        await core.squad("objective", run_id="run-3", dispatch="magic")


@pytest.mark.asyncio
async def test_privileged_run_rejected_before_reading_secrets(monkeypatch):
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: True
    result = await core._dispatch_swarm("do not export", "run-4")
    assert result["status"] == "rejected"
    assert "privileged" in result["swarm"]["reason"]


@pytest.mark.asyncio
async def test_missing_parent_capability_fails_closed_without_local_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("AVRONE_CA_PARENT_CAPABILITY_FILE", raising=False)
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False
    result = await core._dispatch_swarm("objective", "run-5")
    assert result["status"] == "disabled"
    assert "AVRONE_CA_PARENT_CAPABILITY_FILE" in result["swarm"]["reason"]
    assert result["agents"] == []


@pytest.mark.asyncio
async def test_queue_acceptance_is_not_reported_as_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_CA_PARENT_CAPABILITY_FILE", str(tmp_path / "missing.json"))
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False
    result = await core._dispatch_swarm("objective", "run-6")
    assert result["status"] == "disabled"
    assert "completed" not in result["answer"].lower()
    assert result["agents"] == []


@pytest.mark.asyncio
async def test_reused_correlation_with_different_objective_is_rejected(monkeypatch, tmp_path):
    import avrone.swarm_client as client_module
    import avrone.swarm_outbox as outbox_module
    parent = tmp_path / "parent.json"
    parent.write_text('{"capability":{"id":"parent"},"signature":"sig"}')
    parent.chmod(0o600)
    monkeypatch.setenv("AVRONE_CA_PARENT_CAPABILITY_FILE", str(parent))
    monkeypatch.setattr(client_module.CapabilityAuthorityClient, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(client_module.SwarmClient, "from_env", classmethod(lambda cls: SimpleNamespace(audience="swarm")))
    class FakeOutbox:
        def get_by_correlation(self, correlation):
            return {"request": {"payload": {"objective": "different objective"}}}
        def close(self): pass
    monkeypatch.setattr(outbox_module.SwarmOutbox, "from_env", classmethod(lambda cls: FakeOutbox()))
    class ForbiddenDispatcher:
        def __init__(self, *args): pass
        def retry(self, *args): raise AssertionError("must not retry another objective")
    monkeypatch.setattr(outbox_module, "SwarmDispatcher", ForbiddenDispatcher)
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False
    result = await core._dispatch_swarm("new objective", "run-7", "stable-id")
    assert result["status"] == "disabled"
    assert "different objective" in result["swarm"]["reason"]


@pytest.mark.asyncio
async def test_reused_correlation_retries_persisted_request_without_delegation(monkeypatch, tmp_path):
    import avrone.swarm_client as client_module
    import avrone.swarm_outbox as outbox_module
    parent = tmp_path / "parent.json"
    parent.write_text('{"capability":{"id":"parent"},"signature":"sig"}')
    parent.chmod(0o600)
    monkeypatch.setenv("AVRONE_CA_PARENT_CAPABILITY_FILE", str(parent))
    monkeypatch.setattr(client_module.CapabilityAuthorityClient, "from_env", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("must not redelegate"))))
    monkeypatch.setattr(client_module.SwarmClient, "from_env", classmethod(lambda cls: SimpleNamespace(audience="swarm")))
    class FakeOutbox:
        def get_by_correlation(self, correlation):
            return {"request": {"payload": {"objective": "same objective"}}, "message_id": "stable-message"}
        def close(self): pass
    monkeypatch.setattr(outbox_module.SwarmOutbox, "from_env", classmethod(lambda cls: FakeOutbox()))
    class FakeDispatcher:
        def __init__(self, *args): pass
        def retry(self, message_id):
            assert message_id == "stable-message"
            return {"state": "queued", "message_id": message_id}
    monkeypatch.setattr(outbox_module, "SwarmDispatcher", FakeDispatcher)
    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False
    result = await core._dispatch_swarm("same objective", "run-8", "stable-id")
    assert result["status"] == "queued"
    assert "completion" in result["answer"]


@pytest.mark.asyncio
async def test_ca_timeout_restart_reuses_exact_persisted_assertion(monkeypatch, tmp_path):
    import avrone.swarm_client as client_module
    import avrone.swarm_outbox as outbox_module

    parent = tmp_path / "parent.json"
    parent.write_text('{"capability":{"id":"parent"},"signature":"sig"}')
    parent.chmod(0o600)
    monkeypatch.setenv("AVRONE_CA_PARENT_CAPABILITY_FILE", str(parent))

    assertion = {
        "schema": "capability-authority.delegation-assertion/v1",
        "issuer": "spiffe://prod/agent/avrone",
        "subject": "spiffe://prod/agent/avrone",
        "audience": "spiffe://prod/broker",
        "jti": "one-stable-jti",
        "issued_at": "2026-11-03T09:00:00.000Z",
        "expires_at": "2026-11-03T09:00:30.000Z",
        "signature": "same-signed-assertion",
    }
    ca_records = {}
    class FakeAuthority:
        calls = []
        fail_once = True
        def _assertion(self):
            return assertion
        def delegate(self, **kwargs):
            self.calls.append(kwargs)
            if self.fail_once:
                self.fail_once = False
                raise client_module.DelegationUnknownOutcome("simulated timeout", kwargs)
            return {"capability": {"id": "child"}, "signature": "child-signature",
                    "status": "ok", "correlation_id": kwargs["correlation_id"]}
    authority = FakeAuthority()
    monkeypatch.setattr(client_module.CapabilityAuthorityClient, "from_env", classmethod(lambda cls: authority))

    class FakeSwarm:
        audience = "spiffe://prod/agent/swarm"
        def build_request(self, **kwargs):
            return {"envelope": {"message_id": "message-1", "correlation_id": "stable-ca-correlation",
                                 "signature": "signed"},
                    "payload": kwargs["payload"], "capability": kwargs["capability"],
                    "capability_signature": kwargs["capability_signature"]}
    monkeypatch.setattr(client_module.SwarmClient, "from_env", classmethod(lambda cls: FakeSwarm()))

    class FakeOutbox:
        def get_by_correlation(self, correlation):
            return None
        def get_authority_request(self, correlation):
            return ca_records.get(correlation)
        def prepare_authority_request(self, correlation, request):
            previous = ca_records.get(correlation)
            if previous is not None and previous["request"] != request:
                raise AssertionError("persisted CA request must not change")
            record = {"correlation_id": correlation, "request": request}
            ca_records[correlation] = record
            return record
        def close(self):
            pass
    monkeypatch.setattr(outbox_module.SwarmOutbox, "from_env", classmethod(lambda cls: FakeOutbox()))
    class FakeDispatcher:
        def __init__(self, client, outbox):
            pass
        def submit_request(self, request):
            return {"state": "queued", "message_id": request["envelope"]["message_id"]}
    monkeypatch.setattr(outbox_module, "SwarmDispatcher", FakeDispatcher)

    core = TrinityCore.__new__(TrinityCore)
    core.codex = FakeCodex()
    core._privileged_run = lambda: False

    first = await core._dispatch_swarm("same objective", "run-ca-1", "stable-ca-correlation")
    assert first["status"] == "pending"
    assert first["agents"] == []
    persisted = ca_records["stable-ca-correlation"]["request"]["requester_assertion"]
    assert persisted == assertion

    second = await core._dispatch_swarm("same objective", "run-ca-2", "stable-ca-correlation")
    assert second["status"] == "queued"
    assert len(authority.calls) == 2
    assert authority.calls[0]["requester_assertion"] == authority.calls[1]["requester_assertion"] == persisted
    assert authority.calls[0]["correlation_id"] == authority.calls[1]["correlation_id"] == "stable-ca-correlation"
