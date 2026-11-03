import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from avrone import swarm_api
from avrone.swarm_client import UnknownOutcome


class FakeClient:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first
        self._http = SimpleNamespace(close=lambda: None)

    def build_request(self, *, payload, capability, capability_signature, message_id=None, correlation_id=None):
        mid = message_id or "generated-1"
        cid = correlation_id or "generated-c1"
        return {"envelope": {"message_id": mid, "correlation_id": cid, "signature": "signed"},
                "payload": payload, "capability": capability, "capability_signature": capability_signature}

    def send_request(self, request):
        self.calls.append(json.dumps(request, separators=(",", ":")))
        if self.fail_first and len(self.calls) == 1:
            raise UnknownOutcome("timeout", request)
        return {"job": {"id": "job-1", "status": "queued"},
                "receipt": {"message_id": request["envelope"]["message_id"], "signature": "verified"}}

    def verify_receipt(self, receipt, request):
        if receipt.get("message_id") != request["envelope"]["message_id"]:
            raise ValueError("bad receipt")


def setup(tmp_path, *, fail_first=False, configured=True):
    app = FastAPI()
    client = FakeClient(fail_first=fail_first)

    def factory():
        if not configured:
            raise RuntimeError("not configured")
        return client

    def auth(request, token):
        if token != "operator-token":
            raise HTTPException(403, "denied")

    swarm_api.install_routes(app, lambda: None, auth, SimpleNamespace(data_dir=tmp_path),
                             client_factory=factory)
    return app, client


def body(message_id="msg-1", objective="run approved task"):
    return {"objective": objective,
            "capability": {"id": "cap-1", "audience": "spiffe://prod/agent/swarm"},
            "capability_signature": "signed-by-ca",
            "message_id": message_id, "correlation_id": "corr-1"}


def test_enqueue_requires_operator_authentication(tmp_path):
    app, _ = setup(tmp_path)
    with TestClient(app) as http:
        assert http.post("/api/swarm/enqueue", json=body()).status_code == 403


def test_missing_keys_fail_closed_without_local_fallback(tmp_path):
    app, _ = setup(tmp_path, configured=False)
    with TestClient(app) as http:
        r = http.post("/api/swarm/enqueue", json=body(), headers={"X-Admin-Token": "operator-token"})
        assert r.status_code == 503
        assert "local" not in r.text.lower()


def test_enqueue_persists_and_returns_queued_not_completed(tmp_path):
    app, client = setup(tmp_path)
    with TestClient(app) as http:
        r = http.post("/api/swarm/enqueue", json=body(), headers={"X-Admin-Token": "operator-token"})
        assert r.status_code == 202 and r.json()["state"] == "queued"
        assert "completed" not in r.json()
        mid = r.json()["message_id"]
        status = http.get(f"/api/swarm/requests/{mid}", headers={"X-Admin-Token": "operator-token"})
        assert status.json()["state"] == "queued"
        assert len(client.calls) == 1


def test_timeout_retains_request_and_retry_reuses_same_request(tmp_path):
    app, client = setup(tmp_path, fail_first=True)
    with TestClient(app) as http:
        headers = {"X-Admin-Token": "operator-token"}
        first = http.post("/api/swarm/enqueue", json=body(), headers=headers)
        assert first.status_code == 202 and first.json()["state"] == "unknown"
        second = http.post("/api/swarm/retry/msg-1", headers=headers)
        assert second.status_code == 202 and second.json()["state"] == "queued"
        assert client.calls[0] == client.calls[1]


def test_same_id_conflicting_payload_is_rejected(tmp_path):
    app, _ = setup(tmp_path)
    with TestClient(app) as http:
        headers = {"X-Admin-Token": "operator-token"}
        assert http.post("/api/swarm/enqueue", json=body(), headers=headers).status_code == 202
        assert http.post("/api/swarm/enqueue", json=body(objective="different work"), headers=headers).status_code == 409


def test_same_id_same_request_retries_existing_without_reminting(tmp_path):
    app, client = setup(tmp_path, fail_first=True)
    with TestClient(app) as http:
        headers = {"X-Admin-Token": "operator-token"}
        assert http.post("/api/swarm/enqueue", json=body(), headers=headers).json()["state"] == "unknown"
        again = http.post("/api/swarm/enqueue", json=body(), headers=headers)
        assert again.json()["state"] == "queued"
        assert client.calls[0] == client.calls[1]


def test_extra_fields_and_bad_ids_are_rejected(tmp_path):
    app, _ = setup(tmp_path)
    with TestClient(app) as http:
        headers = {"X-Admin-Token": "operator-token"}
        extra = body(); extra["admin_token"] = "leak"
        assert http.post("/api/swarm/enqueue", json=extra, headers=headers).status_code == 422
        bad = body(message_id="../escape")
        assert http.post("/api/swarm/enqueue", json=bad, headers=headers).status_code == 422
        assert http.post("/api/swarm/retry/../escape", headers=headers).status_code in (404, 405, 422)
