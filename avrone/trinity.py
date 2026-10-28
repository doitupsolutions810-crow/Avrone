"""Trinity Core: the daemon kernel that orchestrates agents.

Three stages, Grok Heavy style:
  1. PLAN   - a planner splits the request into parallel angles.
  2. SQUAD  - N agents work their angle at the same time, each with real tools
              (terminal, files, network) and the power to spawn sub-agents.
  3. LEAD   - a lead agent compares every agent's work, cross-checks where
              they disagree, and writes one final answer.

"chat" mode skips the squad and runs one tool-using agent for quick asks.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from .codex import Codex
from .config import Settings
from .llm import LLM, LLMError
from .sovereign import Runner
from .tools import ToolRegistry, builtin_registry

PLANNER_PROMPT = """You are the PLANNER in AVRONE, a multi-agent system.
Split the user's task into {n} independent angles that different agents can work
on IN PARALLEL (e.g. research, implementation, verification, devil's advocate).
Every angle must be useful alone; do not make one depend on another.
Reply with JSON only: {{"subtasks": [{{"role": "<short role name>", "focus": "<what this agent should do>"}}]}}"""

WORKER_PROMPT = """You are {name}, the {role} agent in AVRONE, working inside a real Linux
environment with a workspace directory. Overall task from the user:
{task}

Your focus: {focus}

{tools}
Reply with ONE fenced JSON block per turn, either a tool call:
```json
{{"tool": "<name>", "args": {{...}}}}
```
or, when you are done, your final result:
```json
{{"final": "<your complete findings, including any code, commands run and results>"}}
```
Be concrete. Verify claims with tools when you can. You have at most {steps} turns."""

LEAD_PROMPT = """You are the LEAD agent in AVRONE. Several agents worked the same task in
parallel from different angles. Compare their work like a study group:
- keep what is verified or agreed on,
- where they disagree, reason out which is right (say so briefly),
- drop anything unsupported,
then write ONE final answer for the user. Answer the task directly; do not
describe the agents unless it helps."""


@dataclass
class AgentContext:
    core: "TrinityCore"
    run_id: str
    name: str
    depth: int


def parse_action(text: str) -> dict[str, Any] | None:
    """Pull the JSON action out of a model reply (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    b = re.search(r"\{.*\}", text, re.S)
    if b:
        candidates.append(b.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and ("tool" in obj or "final" in obj):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class TrinityCore:
    def __init__(self, settings: Settings, llm: LLM, runner: Runner, codex: Codex,
                 tools: ToolRegistry | None = None):
        self.settings = settings
        self.tools = tools or builtin_registry()
        self.llm = llm
        self.runner = runner
        self.codex = codex
        self._counter = 0
        self.active: dict[str, dict[str, Any]] = {}  # agent name -> status, for the HUD

    def _name(self, role: str) -> str:
        self._counter += 1
        return f"{re.sub(r'[^A-Za-z0-9]', '', role)[:16] or 'Agent'}-{self._counter}"

    # ------------------------------------------------------------ one agent
    async def run_agent(self, run_id: str, role: str, task: str, focus: str | None = None,
                        parent: str | None = None, depth: int = 0) -> str:
        name = self._name(role)
        ctx = AgentContext(self, run_id, name, depth)
        self.active[name] = {"role": role, "status": "working", "parent": parent, "run_id": run_id}
        self.codex.emit(run_id, "agent.spawn", name, role=role, focus=focus or task, parent=parent, depth=depth)
        messages = [
            {"role": "system", "content": WORKER_PROMPT.format(
                name=name, role=role, task=task, focus=focus or task, tools=self.tools.docs(),
                steps=self.settings.max_steps)},
            {"role": "user", "content": "Begin."},
        ]
        result = ""
        try:
            for step in range(self.settings.max_steps):
                reply = await self.llm.chat(messages, session=f"avrone:{run_id}:{name}")
                messages.append({"role": "assistant", "content": reply})
                action = parse_action(reply)
                if action is None:
                    # Model answered in prose: accept it as the final result.
                    result = reply.strip()
                    break
                if "final" in action:
                    result = str(action["final"])
                    break
                tool, args = str(action.get("tool")), action.get("args") or {}
                self.codex.emit(run_id, "tool.call", name, tool=tool, args=args, step=step)
                out = await self.tools.call(ctx, tool, args if isinstance(args, dict) else {})
                self.codex.emit(run_id, "tool.result", name, tool=tool, output=out[:4000])
                messages.append({"role": "user", "content": f"TOOL RESULT ({tool}):\n{out}"})
            else:
                messages.append({"role": "user", "content": "Out of turns. Give your final result now as plain text."})
                result = await self.llm.chat(messages, session=f"avrone:{run_id}:{name}")
        except LLMError as e:
            result = f"[{name} failed: {e}]"
            self.codex.emit(run_id, "error", name, message=str(e))
        self.active[name]["status"] = "done"
        self.codex.emit(run_id, "agent.done", name, result=result[:6000])
        return result

    # ------------------------------------------------------------ the squad
    async def plan(self, run_id: str, task: str, n: int) -> list[dict[str, str]]:
        self.codex.emit(run_id, "lead.plan", "Planner", squad_size=n)
        reply = await self.llm.chat([
            {"role": "system", "content": PLANNER_PROMPT.format(n=n)},
            {"role": "user", "content": f"TASK: {task}"},
        ], temperature=0.4, session=f"avrone:{run_id}:planner")
        try:
            m = re.search(r"\{.*\}", reply, re.S)
            subtasks = json.loads(m.group(0))["subtasks"] if m else []
        except (json.JSONDecodeError, KeyError, TypeError):
            subtasks = []
        subtasks = [s for s in subtasks if isinstance(s, dict) and s.get("focus")][:n]
        if not subtasks:  # fallback: same task, independent attempts (pure Heavy style)
            subtasks = [{"role": f"Solver{i + 1}", "focus": "Solve the whole task independently."} for i in range(n)]
        self.codex.emit(run_id, "lead.planned", "Planner", subtasks=subtasks)
        return subtasks

    async def squad(self, task: str, n: int | None = None, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex[:10]
        n = max(1, min(n or self.settings.squad_size, 8))
        self.codex.emit(run_id, "run.start", "Trinity", prompt=task, mode="squad", squad_size=n)
        try:
            subtasks = await self.plan(run_id, task, n)
        except LLMError as e:
            self.codex.emit(run_id, "error", "Planner", message=str(e))
            self.codex.emit(run_id, "run.done", "Trinity", answer=f"Model backend error: {e}")
            return {"run_id": run_id, "answer": f"Model backend error: {e}", "agents": []}

        results = await asyncio.gather(*[
            self.run_agent(run_id, s.get("role", "Agent"), task, focus=s["focus"]) for s in subtasks
        ])
        reports = "\n\n".join(
            f"### Agent {i + 1}: {s.get('role')} (focus: {s['focus']})\n{r}"
            for i, (s, r) in enumerate(zip(subtasks, results)))
        self.codex.emit(run_id, "lead.merge", "Lead", agents=len(results))
        try:
            answer = await self.llm.chat([
                {"role": "system", "content": LEAD_PROMPT},
                {"role": "user", "content": f"TASK:\n{task}\n\nAGENT REPORTS:\n{reports}"},
            ], model=self.settings.lead_model or None, temperature=0.3, session=f"avrone:{run_id}:lead")
        except LLMError as e:
            answer = f"Lead failed ({e}). Raw agent reports:\n\n{reports}"
        self.codex.emit(run_id, "run.done", "Trinity", answer=answer)
        return {"run_id": run_id, "answer": answer,
                "agents": [{"role": s.get("role"), "focus": s["focus"], "result": r} for s, r in zip(subtasks, results)]}

    async def chat(self, task: str, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex[:10]
        self.codex.emit(run_id, "run.start", "Trinity", prompt=task, mode="chat")
        answer = await self.run_agent(run_id, "Assistant", task)
        self.codex.emit(run_id, "run.done", "Trinity", answer=answer)
        return {"run_id": run_id, "answer": answer, "agents": []}
