import json
from types import SimpleNamespace

import httpx
import pytest

import avrone.tools_extra as tools_extra


def _ctx():
    return SimpleNamespace(
        core=SimpleNamespace(settings=SimpleNamespace(allow_network=False), codex=None),
        run_id="bridge-test",
        name="bridge-test",
    )


@pytest.mark.asyncio
async def test_grokbot_ask_uses_fixed_research_agent_with_network_policy_disabled(monkeypatch):
    monkeypatch.setenv("AVRONE_GROKBOT_URL", "http://grok-bot:3000")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"answer": "Verified from Grok Bot."})

    monkeypatch.setattr(tools_extra, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    result = await tools_extra.t_grokbot_ask(_ctx(), {"prompt": "Check the deployment status."})

    payload = json.loads(result)
    assert payload == {"ok": True, "agent": "grok-researcher", "answer": "Verified from Grok Bot."}
    assert seen["url"] == "http://grok-bot:3000/api/chat"
    assert seen["payload"] == {
        "agentId": "grok-researcher",
        "mode": "chat",
        "message": "Check the deployment status.",
    }


@pytest.mark.asyncio
async def test_grokbot_ask_is_disabled_without_operator_config(monkeypatch):
    monkeypatch.delenv("AVRONE_GROKBOT_URL", raising=False)
    result = await tools_extra.t_grokbot_ask(_ctx(), {"prompt": "Hello"})
    assert "bridge is disabled" in result


@pytest.mark.asyncio
async def test_grokbot_ask_blocks_redirects(monkeypatch):
    monkeypatch.setenv("AVRONE_GROKBOT_URL", "http://grok-bot:3000")
    monkeypatch.setattr(
        tools_extra,
        "HTTP_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/"})),
    )
    result = await tools_extra.t_grokbot_ask(_ctx(), {"prompt": "Do not follow redirects"})
    assert "redirects are blocked" in result


@pytest.mark.asyncio
async def test_grokbot_ask_rejects_non_origin_url(monkeypatch):
    monkeypatch.setenv("AVRONE_GROKBOT_URL", "http://grok-bot:3000/admin")
    result = await tools_extra.t_grokbot_ask(_ctx(), {"prompt": "Hello"})
    assert "must be an HTTP(S) origin" in result


@pytest.mark.asyncio
async def test_grokbot_ask_caps_prompt_length(monkeypatch):
    monkeypatch.setenv("AVRONE_GROKBOT_URL", "http://grok-bot:3000")
    result = await tools_extra.t_grokbot_ask(_ctx(), {"prompt": "x" * 8001})
    assert "8000-character limit" in result
