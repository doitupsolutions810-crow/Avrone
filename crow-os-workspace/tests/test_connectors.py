"""Connectors stay quiet when their keys are absent, and call the documented APIs when set."""

import httpx
import pytest

from crow_os.connectors import ConnectorUnavailable, cursor_status, launch_cursor_agent, xai_status


class ExplodingClient(httpx.Client):
    def request(self, *args, **kwargs):
        raise AssertionError("network was called")


def test_missing_keys_do_not_call_out(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    client = ExplodingClient()
    assert cursor_status(client=client)["state"] == "missing_key"
    assert "CURSOR_API_KEY" in cursor_status(client=client)["detail"]
    assert xai_status(client=client)["state"] == "missing_key"
    with pytest.raises(ConnectorUnavailable):
        launch_cursor_agent(prompt="hi", repository="https://github.com/acme/widgets", ref="main", client=client)


def test_cursor_launch_shape(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    seen = {}

    class Fake:
        def request(self, method, url, headers=None, json=None):
            seen["method"] = method
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json

            class Response:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"agent": {"id": "bc-1"}, "run": {"id": "run-1", "status": "FINISHED"}}

            return Response()

        def close(self):
            return None

    payload = launch_cursor_agent(
        prompt="run the edge suite",
        repository="https://github.com/acme/widgets",
        ref="main",
        client=Fake(),
    )
    assert payload["agent"]["id"] == "bc-1"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.cursor.com/v1/agents"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["json"]["autoCreatePR"] is False
    assert seen["json"]["workOnCurrentBranch"] is False
    assert seen["json"]["repos"][0]["url"] == "https://github.com/acme/widgets"
    assert "test-key" not in str(seen["json"])
