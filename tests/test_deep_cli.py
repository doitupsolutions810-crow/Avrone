import os

os.environ["AVRONE_LLM_MODE"] = "mock"
os.environ["AVRONE_RUNNER"] = "local"
os.environ.setdefault("AVRONE_ADMIN_TOKEN", "test-token")

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from avrone.bootstrap import build_core, run_cleanups
from avrone.config import PROVIDERS, Settings, resolve_provider
from avrone.llm import LLM, LLMError
from avrone.trinity import DEEP_ANGLES

ROOT = Path(__file__).resolve().parents[1]
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _settings(tmp_path: Path, **kw) -> Settings:
    s = Settings()
    s.llm_mode = "mock"
    s.runner = "local"
    s.workspace = tmp_path / "ws"
    s.data_dir = tmp_path / "data"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


async def _with_core(tmp_path, fn, **kw):
    core, cleanups = await build_core(_settings(tmp_path, **kw), extras=False)
    try:
        return core, await fn(core)
    finally:
        await run_cleanups(cleanups)


# ---------------------------------------------------------------- deep mode
def test_deep_with_first_answer(tmp_path):
    core, r = asyncio.run(_with_core(tmp_path, lambda c: c.deep("Is the sky blue?", "Yes, Rayleigh scattering.")))
    assert r["mode"] == "deep" and r["first_answer"] == "Yes, Rayleigh scattering."
    for section in ("## Verdict", "## Corrections", "## Additions", "## Counterpoints", "## Sources", "## Bottom line"):
        assert section in r["report"]
    assert "4 reviewers" in r["report"]
    assert [a["role"] for a in r["agents"]] == [a["role"] for a in DEEP_ANGLES[:4]]
    assert all("review" in a["result"] for a in r["agents"])
    log = core.codex.replay(r["run_id"])
    kinds = [e["kind"] for e in log]
    assert kinds[0] == "run.start" and log[0]["data"]["mode"] == "deep" and kinds[-1] == "run.done"
    assert "lead.plan" in kinds and "lead.merge" in kinds
    assert kinds.count("agent.spawn") == 4
    assert "first_run_id" not in r


def test_deep_without_first_answer_runs_squad_first(tmp_path):
    core, r = asyncio.run(_with_core(tmp_path, lambda c: c.deep("build a todo app", n=6)))
    assert r["first_answer"].startswith("FINAL ANSWER")  # the squad's lead answer
    assert len(r["agents"]) == 6 and r["agents"][-1]["role"] == "Synthesizer-of-Sources"
    first_log = core.codex.replay(r["first_run_id"])
    assert first_log[0]["data"]["mode"] == "squad"
    assert any(e["kind"] == "deep.first_wave_done" for e in core.codex.replay(r["run_id"]))


def test_deep_size_is_capped(tmp_path):
    _, r = asyncio.run(_with_core(tmp_path, lambda c: c.deep("q", "a", n=50)))
    assert len(r["agents"]) == len(DEEP_ANGLES) == 6


def test_semaphore_bounds_parallel_llm_calls(tmp_path):
    state = {"now": 0, "peak": 0}

    async def go(core):
        real = core.llm.chat

        async def slow(messages, **kw):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return await real(messages, **kw)

        core.llm.chat = slow
        return await core.deep("q", "a", n=6)

    asyncio.run(_with_core(tmp_path, go, max_parallel=2))
    assert state["peak"] == 2


# ---------------------------------------------------------------- server
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def test_api_deep_tools_extensions(client):
    r = client.post("/api/chat", json={"message": "q", "mode": "deep", "first_answer": "a", "squad_size": 2}).json()
    assert r["mode"] == "deep" and "## Verdict" in r["report"] and len(r["agents"]) == 2
    tools = client.get("/api/tools").json()
    assert {"shell", "read_file", "write_file", "fetch", "spawn"} <= {t["name"] for t in tools}
    assert all("source" in t for t in tools)
    ext = client.get("/api/extensions").json()
    assert {"extra_tools", "plugins", "mcp", "errors"} <= set(ext)
    assert client.post("/api/chat", json={"message": "q", "mode": "nope"}).status_code == 400


# ---------------------------------------------------------------- provider presets
def test_provider_presets():
    env = {"XAI_API_KEY": "xai-k", "OPENCLAW_GATEWAY_TOKEN": "oc-k"}
    assert resolve_provider(None, env=env) == ("openclaw", "http://127.0.0.1:18789/v1", "openclaw", "oc-k")
    assert resolve_provider("xai", env=env) == ("xai", "https://api.x.ai/v1", "grok-4", "xai-k")
    assert resolve_provider("openai", env={})[1:3] == ("https://api.openai.com/v1", "gpt-4.1")
    assert resolve_provider("OpenRouter", env={})[1] == "https://openrouter.ai/api/v1"
    assert resolve_provider("ollama", env={})[1:] == ("http://127.0.0.1:11434/v1", "llama3.1", "")
    # explicit values override the preset
    assert resolve_provider("xai", "http://proxy/v1", "grok-3", "explicit", env=env) == \
        ("xai", "http://proxy/v1", "grok-3", "explicit")
    assert resolve_provider("custom", "http://my/v1", env={})[1] == "http://my/v1"
    with pytest.raises(ValueError):
        resolve_provider("custom", env={})
    with pytest.raises(ValueError):
        resolve_provider("bogus", env={})
    assert set(PROVIDERS) == {"openclaw", "xai", "openai", "openrouter", "ollama", "custom"}


def test_settings_reads_provider_env(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.delenv("AVRONE_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("AVRONE_LLM_MODEL", "qwen2.5")
    monkeypatch.setenv("AVRONE_LLM_API_KEY", "")
    s = Settings()
    assert (s.provider, s.llm_base_url, s.llm_model, s.llm_api_key) == \
        ("ollama", "http://127.0.0.1:11434/v1", "qwen2.5", "")


# ---------------------------------------------------------------- retries
def _live_settings(**kw) -> Settings:
    s = Settings()
    s.llm_mode, s.provider, s.llm_base_url, s.llm_api_key = "live", "custom", "http://llm.test/v1", ""
    s.llm_backoff = 0.0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _patch_transport(monkeypatch, handler):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_llm_retries_429_then_succeeds(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429 if len(calls) == 1 else 503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _patch_transport(monkeypatch, handler)
    assert asyncio.run(LLM(_live_settings()).chat([{"role": "user", "content": "hi"}])) == "ok"
    assert len(calls) == 3
    assert "authorization" not in calls[0].headers  # empty key is allowed: no auth header


def test_llm_gives_up_after_3_tries_and_no_retry_on_400(monkeypatch):
    calls = []
    _patch_transport(monkeypatch, lambda req: (calls.append(1), httpx.Response(500, text="boom"))[1])
    with pytest.raises(LLMError, match="All upstreams failed"):
        asyncio.run(LLM(_live_settings()).chat([{"role": "user", "content": "hi"}]))
    assert len(calls) == 3
    calls.clear()
    _patch_transport(monkeypatch, lambda req: (calls.append(1), httpx.Response(400, text="bad"))[1])
    with pytest.raises(LLMError, match="400"):
        asyncio.run(LLM(_live_settings()).chat([{"role": "user", "content": "hi"}]))
    assert len(calls) == 1


# ---------------------------------------------------------------- CLI
def _cli(tmp_path, *args):
    env = {**os.environ, "AVRONE_LLM_MODE": "mock", "AVRONE_RUNNER": "local",
           "AVRONE_WORKSPACE": str(tmp_path / "ws"), "AVRONE_DATA_DIR": str(tmp_path / "data"),
           "AVRONE_PLUGINS_DIR": str(tmp_path / "noplugins"), "AVRONE_MCP_CONFIG": str(tmp_path / "nomcp.json"),
           "AVRONE_ENV_FILE": str(tmp_path / "none.env"), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "avrone.cli", *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)


def test_cli_deep_with_first_answer_file_and_json(tmp_path):
    fa = tmp_path / "first.md"
    fa.write_text("Paris is the capital of France.")
    out = tmp_path / "result.json"
    p = _cli(tmp_path, "deep", "capital of France?", "--first-answer-file", str(fa), "-n", "3", "--json", str(out))
    assert p.returncode == 0, p.stderr
    assert "## Verdict" in p.stdout and "3 reviewers" in p.stdout
    assert "run.start" in p.stderr and "run.done" in p.stderr  # live event lines
    data = json.loads(out.read_text())
    assert data["mode"] == "deep" and data["first_answer"] == "Paris is the capital of France."
    assert len(data["agents"]) == 3


def test_cli_quiet_squad_tools_runs(tmp_path):
    p = _cli(tmp_path, "squad", "build a todo app", "--quiet")
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith("FINAL ANSWER") and "run.start" not in p.stderr
    p = _cli(tmp_path, "deep", "q", "--first-answer", "a", "--quiet")
    assert p.returncode == 0 and "## Bottom line" in p.stdout
    p = _cli(tmp_path, "tools")
    assert p.returncode == 0 and "shell" in p.stdout and "[builtin]" in p.stdout
    p = _cli(tmp_path, "runs")
    assert p.returncode == 0 and len(p.stdout.strip().splitlines()) == 2


def test_cli_deep_rounds_flag(tmp_path):
    out = tmp_path / "r.json"
    p = _cli(tmp_path, "deep", "q", "--first-answer", "a", "-n", "2", "--rounds", "3", "--quiet", "--json", str(out))
    assert p.returncode == 0, p.stderr
    data = json.loads(out.read_text())
    # the stock mock lead reports no corrections, so the run converges after one wave
    assert data["max_rounds"] == 3 and data["rounds_run"] == 1 and data["converged"]
    assert "Rounds run: 1 of 3" in p.stdout and "Confidence: Med" in p.stdout


def test_cli_deep_rounds_capped_and_env(tmp_path, monkeypatch):
    out = tmp_path / "r.json"
    p = _cli(tmp_path, "deep", "q", "--first-answer", "a", "-n", "1", "--rounds", "40", "-q", "--json", str(out))
    assert p.returncode == 0, p.stderr
    assert json.loads(out.read_text())["max_rounds"] == 5
    monkeypatch.setenv("AVRONE_DEEP_ROUNDS", "2")
    p = _cli(tmp_path, "deep", "q", "--first-answer", "a", "-n", "1", "-q", "--json", str(out))
    assert p.returncode == 0, p.stderr
    assert json.loads(out.read_text())["max_rounds"] == 2 and "Rounds run: 1 of 2" in p.stdout


def test_cli_deep_help_lists_rounds(tmp_path):
    p = _cli(tmp_path, "deep", "--help")
    assert p.returncode == 0 and "--rounds" in p.stdout
