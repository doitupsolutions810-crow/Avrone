"""Model client. Talks to any OpenAI-compatible /chat/completions endpoint.

Default target is the OpenClaw Gateway (enable
gateway.http.endpoints.chatCompletions.enabled = true in OpenClaw's config).
Point AVRONE_LLM_BASE_URL at xAI, OpenAI, Ollama, LM Studio, vLLM, etc. to use
those instead. AVRONE_LLM_MODE=mock runs a scripted model for demos/tests.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import Settings


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: Settings):
        self.s = settings

    async def chat(self, messages: list[dict[str, str]], model: str | None = None,
                   temperature: float = 0.7, session: str | None = None) -> str:
        if self.s.llm_mode == "mock":
            return _mock_reply(messages)
        headers = {"Content-Type": "application/json"}
        if self.s.llm_api_key:
            headers["Authorization"] = f"Bearer {self.s.llm_api_key}"
        if session:
            # OpenClaw: keep each agent in its own gateway session.
            headers["x-openclaw-session-key"] = session
        body: dict[str, Any] = {
            "model": model or self.s.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        url = self.s.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.s.llm_timeout) as client:
                r = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Model backend unreachable at {url}: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"Model backend error {r.status_code}: {r.text[:500]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"Unexpected model response: {r.text[:500]}") from e


# ---------------------------------------------------------------- mock model
def _mock_reply(messages: list[dict[str, str]]) -> str:
    system = messages[0]["content"] if messages else ""
    last = messages[-1]["content"] if messages else ""
    if "You are the PLANNER" in system:
        task = re.search(r"TASK:\s*(.*)", last, re.S)
        t = (task.group(1).strip() if task else "the task")[:80]
        return json.dumps({"subtasks": [
            {"role": "Researcher", "focus": f"Gather facts relevant to: {t}"},
            {"role": "Engineer", "focus": f"Work out a concrete solution for: {t}"},
            {"role": "Critic", "focus": f"Find flaws and risks in approaches to: {t}"},
        ]})
    if "You are the LEAD" in system:
        return "FINAL ANSWER (mock): merged the squad's findings into one answer."
    # Worker: use one tool on the first step, then answer.
    if "TOOL RESULT" not in last:
        return '```json\n{"tool": "shell", "args": {"cmd": "echo avrone-online && uname -s"}}\n```'
    return '```json\n{"final": "Mock worker result: environment checked, findings ready."}\n```'
