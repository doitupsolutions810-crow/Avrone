"""Chat agents whose tools stay inside one git worktree.

Patches are registered as changes and stay unmerged and unverified until the
workspace gates pass. Model replies come only from Ollama, xAI, or Cursor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from . import gitrepo
from .connectors import (
    ConnectorUnavailable,
    cursor_run,
    cursor_status,
    github_https_url,
    iter_xai_chat,
    launch_cursor_agent,
    xai_status,
)
from .ollama_client import iter_chat, list_models, ollama_binary, ollama_host, probe_ollama
from .util import is_relative_test_path, utc_now

Publish = Callable[[str, dict[str, Any]], None]
DENIED_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "credentials.json", "secrets.json"}
MAX_STEPS = 8

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files inside the agent worktree.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file inside the agent worktree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file inside the worktree. Refuses secret filenames. Does not verify the change.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_patch",
            "description": "Commit the worktree on its side branch. The change stays unmerged and unverified.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run pytest on worktree files. Requires a clean worktree. There is no harness timeout.",
            "parameters": {
                "type": "object",
                "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                "required": ["paths"],
            },
        },
    },
]

SYSTEM = (
    "You are a Crow-OS AVRONE agent. Use only the provided tools, and only inside this git worktree. "
    "To change code: write_file, then propose_patch, then run_tests. "
    "propose_patch commits on a side branch. The workspace keeps that change unmerged and unverified "
    "until its acceptance gates pass. Never claim a change is verified or merged. "
    "Do not read or write secrets, .env files, keys, or credentials."
)


def resolve_inside(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path is required")
    if raw.startswith(("/", "\\")) or "\x00" in raw:
        raise ValueError("path must stay inside the worktree")
    parts = Path(raw).parts
    if any(part == ".." for part in parts):
        raise ValueError("path must stay inside the worktree")
    root_resolved = root.resolve()
    target = (root / raw).resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError("path must stay inside the worktree")
    return target


class ChatWorkspace:
    def __init__(self, workspace: Any, publish: Publish | None = None):
        self.ws = workspace
        self.publish = publish or (lambda _thread_id, _event: None)
        self._flags: dict[str, threading.Event] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def connectors(self) -> dict[str, Any]:
        models = list_models()
        return {
            "ollama": {
                "host": ollama_host(),
                "binary": ollama_binary(),
                "state": models.get("state"),
                "detail": models.get("detail"),
                "error": models.get("error") or "",
                "models": models.get("installed") or [],
            },
            "cursor": cursor_status(),
            "xai": xai_status(),
        }

    def list_agents(self) -> list[dict[str, Any]]:
        self._ensure_local_agent()
        return self.ws.store.query("SELECT * FROM chat_agents ORDER BY created_at")

    def create_agent(self, *, name: str, backend: str, model: str = "") -> dict[str, Any]:
        if backend not in {"ollama", "cursor", "xai"}:
            raise ValueError("backend must be ollama, cursor, or xai")
        if not name.strip():
            raise ValueError("name is required")
        agent_id = "agt-" + uuid.uuid4().hex[:10]
        self.ws.store.execute(
            "INSERT INTO chat_agents (id, name, backend, model, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, name.strip(), backend, model.strip(), utc_now()),
        )
        self.ws.store.event("chat_agent", {"id": agent_id, "backend": backend, "model": model.strip()})
        return self.ws.store.query_one("SELECT * FROM chat_agents WHERE id=?", (agent_id,))

    def set_model(self, agent_id: str, model: str) -> dict[str, Any]:
        if not self.ws.store.query_one("SELECT id FROM chat_agents WHERE id=?", (agent_id,)):
            raise KeyError(agent_id)
        self.ws.store.execute("UPDATE chat_agents SET model=? WHERE id=?", (model.strip(), agent_id))
        self.ws.store.event("chat_agent_model", {"id": agent_id, "model": model.strip()})
        return self.ws.store.query_one("SELECT * FROM chat_agents WHERE id=?", (agent_id,))

    def list_threads(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        if agent_id:
            return self.ws.store.query(
                "SELECT * FROM chat_threads WHERE agent_id=? ORDER BY created_at DESC",
                (agent_id,),
            )
        return self.ws.store.query("SELECT * FROM chat_threads ORDER BY created_at DESC")

    def create_thread(self, agent_id: str, title: str = "") -> dict[str, Any]:
        agent = self.ws.store.query_one("SELECT * FROM chat_agents WHERE id=?", (agent_id,))
        if agent is None:
            raise KeyError(agent_id)
        thread_id = "t" + uuid.uuid4().hex[:10]
        branch = f"crow-os/chat-{thread_id}"
        worktree = self.ws.runner.worktree_root / f"chat-{thread_id}"
        sha = gitrepo.head_sha(self.ws.repo)
        self.ws.store.execute(
            """
            INSERT INTO chat_threads
                (id, agent_id, title, branch, base_sha, worktree, change_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, '', 'idle', ?)
            """,
            (thread_id, agent_id, (title or "New thread").strip(), branch, sha, str(worktree), utc_now()),
        )
        self.ws.store.event("chat_thread", {"id": thread_id, "agent_id": agent_id, "branch": branch, "base_sha": sha})
        return self.thread_detail(thread_id)

    def thread_detail(self, thread_id: str) -> dict[str, Any]:
        thread = self.ws.store.query_one("SELECT * FROM chat_threads WHERE id=?", (thread_id,))
        if thread is None:
            raise KeyError(thread_id)
        thread["messages"] = self.ws.store.query(
            "SELECT * FROM chat_messages WHERE thread_id=? ORDER BY id",
            (thread_id,),
        )
        thread["activities"] = self.ws.store.query(
            "SELECT * FROM chat_activities WHERE thread_id=? ORDER BY id",
            (thread_id,),
        )
        if thread.get("change_id"):
            try:
                thread["change"] = self.ws.evaluation(thread["change_id"])
            except KeyError:
                thread["change"] = None
        return thread

    def post_message(self, thread_id: str, text: str, *, background: bool = True) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("message is empty")
        thread = self.ws.store.query_one("SELECT * FROM chat_threads WHERE id=?", (thread_id,))
        if thread is None:
            raise KeyError(thread_id)
        self.ws.store.execute(
            "INSERT INTO chat_messages (thread_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (thread_id, text.strip(), utc_now()),
        )
        self.ws.store.execute("UPDATE chat_threads SET status='running' WHERE id=?", (thread_id,))
        self.ws.store.event("chat_message", {"thread_id": thread_id, "role": "user"})
        if background:
            threading.Thread(target=self._guarded_turn, args=(thread_id,), name=f"chat-{thread_id}", daemon=True).start()
        else:
            self.run_turn(thread_id)
        return self.thread_detail(thread_id)

    def cancel(self, thread_id: str) -> dict[str, str]:
        flag = self._flags.setdefault(thread_id, threading.Event())
        flag.set()
        proc = self._procs.get(thread_id)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, 15)
            except (ProcessLookupError, PermissionError):
                pass
        self._activity(thread_id, "status", "cancel requested", "operator cancelled the agent turn")
        return {"id": thread_id, "cancel": "requested"}

    def run_turn(self, thread_id: str) -> None:
        flag = self._flags.setdefault(thread_id, threading.Event())
        flag.clear()
        thread = self.ws.store.query_one("SELECT * FROM chat_threads WHERE id=?", (thread_id,))
        agent = self.ws.store.query_one("SELECT * FROM chat_agents WHERE id=?", (thread["agent_id"],))
        self.publish(thread_id, {"type": "status", "status": "running"})
        try:
            if agent["backend"] == "ollama":
                self._model_turn(thread, agent, flag, backend="ollama")
            elif agent["backend"] == "xai":
                self._model_turn(thread, agent, flag, backend="xai")
            elif agent["backend"] == "cursor":
                self._cursor_turn(thread, agent, flag)
            else:
                self._assistant(thread_id, f"Unknown backend {agent['backend']}.")
            status = "cancelled" if flag.is_set() else "idle"
        except Exception as exc:
            self._assistant(thread_id, f"{type(exc).__name__}: {exc}")
            status = "failed"
        self.ws.store.execute("UPDATE chat_threads SET status=? WHERE id=?", (status, thread_id))
        self.publish(thread_id, {"type": "status", "status": status})
        self.ws.store.event("chat_turn_finished", {"thread_id": thread_id, "status": status})

    def _guarded_turn(self, thread_id: str) -> None:
        try:
            self.run_turn(thread_id)
        except Exception as exc:
            self.ws.store.event("chat_thread_error", {"thread_id": thread_id, "error": str(exc)})

    def _model_turn(self, thread: dict[str, Any], agent: dict[str, Any], flag: threading.Event, *, backend: str) -> None:
        if backend == "ollama":
            probe = probe_ollama()
            binary = ollama_binary()
            if probe["state"] != "reachable":
                where = f"binary {binary['path']}" if binary.get("path") else "ollama binary not on PATH"
                self._assistant(
                    thread["id"],
                    f"Ollama unreachable at {probe['host']} ({where}). No model reply was generated.",
                )
                return
            model = agent.get("model") or (probe["models"][0] if probe["models"] else "")
            if not model:
                self._assistant(thread["id"], "Ollama is reachable but no model is installed. Pull one from Settings.")
                return
        else:
            status = xai_status()
            if status["state"] != "ok":
                self._assistant(thread["id"], status["detail"])
                return
            model = agent.get("model") or os.environ.get("XAI_MODEL") or "grok-4.7"
        sandbox = self._sandbox(thread)
        messages = [{"role": "system", "content": SYSTEM}, *self._model_messages(thread["id"])]
        for _step in range(MAX_STEPS):
            if flag.is_set():
                return
            content, calls, error = self._complete(backend, model, messages, thread["id"])
            if error:
                self._assistant(thread["id"], error)
                return
            if calls:
                if content:
                    self._assistant(thread["id"], content)
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {"function": {"name": call.get("name") or "", "arguments": call.get("arguments") or {}}}
                            for call in calls
                        ],
                    }
                )
                for call in calls:
                    if flag.is_set():
                        return
                    result = self._dispatch(thread, sandbox, call)
                    payload = json.dumps(result, default=str)
                    self.ws.store.execute(
                        "INSERT INTO chat_messages (thread_id, role, content, created_at) VALUES (?, 'tool', ?, ?)",
                        (thread["id"], payload, utc_now()),
                    )
                    messages.append({"role": "tool", "content": payload, "name": call.get("name") or ""})
                continue
            if content:
                self._assistant(thread["id"], content)
            return
        self._assistant(thread["id"], "Stopped after 8 tool steps. Send another message to continue.")

    def _complete(self, backend: str, model: str, messages: list[dict[str, Any]], thread_id: str) -> tuple[str, list[dict[str, Any]], str]:
        stream: Iterator[dict[str, Any]]
        if backend == "ollama":
            stream = iter_chat(ollama_host(), model, messages, TOOLS)
        else:
            stream = iter_xai_chat(model=model, messages=messages, tools=TOOLS)
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for event in stream:
            if event["type"] == "token":
                parts.append(event["text"])
                self.publish(thread_id, {"type": "token", "text": event["text"]})
            elif event["type"] == "tool":
                calls.append(event["call"])
            elif event["type"] == "error":
                return "", [], event.get("error") or "model request failed"
        return "".join(parts), calls, ""

    def _cursor_turn(self, thread: dict[str, Any], agent: dict[str, Any], flag: threading.Event) -> None:
        status = cursor_status()
        if status["state"] != "ok":
            self._assistant(thread["id"], status["detail"])
            return
        remote = ""
        try:
            remote = github_https_url(gitrepo.remote_url(self.ws.repo))
        except gitrepo.GitError:
            remote = ""
        prompt = self._latest_user(thread["id"])
        try:
            launched = launch_cursor_agent(prompt=prompt, repository=remote, ref=thread.get("base_sha") or "main")
        except ConnectorUnavailable as exc:
            self._assistant(thread["id"], str(exc))
            return
        agent_info = launched.get("agent") or {}
        run = launched.get("run") or {}
        agent_id = agent_info.get("id") or ""
        run_id = run.get("id") or agent_info.get("latestRunId") or ""
        url = agent_info.get("url") or ""
        self._activity(thread["id"], "status", "Cursor cloud agent launched", json.dumps({"id": agent_id, "run": run_id, "url": url}))
        self._assistant(thread["id"], f"Cursor cloud agent {agent_id} started. {url}".strip())
        if not agent_id or not run_id:
            return
        while not flag.is_set():
            try:
                current = cursor_run(agent_id, run_id)
            except (ConnectorUnavailable, Exception) as exc:  # surface the real API error
                self._assistant(thread["id"], f"Cursor run check failed: {exc}")
                return
            run_status = current.get("status") or ""
            self.publish(thread["id"], {"type": "log", "line": f"cursor run {run_status}\n"})
            if run_status not in {"CREATING", "RUNNING", "WAITING_FOR_BACKGROUND_WORK"}:
                result = current.get("result") or ""
                git_info = current.get("git") or {}
                self._activity(thread["id"], "status", f"Cursor run {run_status}", json.dumps(git_info)[:4000])
                if result:
                    self._assistant(thread["id"], result)
                note = (
                    "A cloud-agent branch or pull request stays unverified in this workspace "
                    "until the acceptance gates pass."
                )
                self._assistant(thread["id"], note)
                return
            flag.wait(3)

    def _dispatch(self, thread: dict[str, Any], sandbox: "Sandbox", call: dict[str, Any]) -> dict[str, Any]:
        name = call.get("name") or ""
        arguments = call.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            if name == "list_files":
                result = sandbox.list_files(str(arguments.get("path") or "."))
            elif name == "read_file":
                result = {"path": arguments.get("path"), "text": sandbox.read_file(str(arguments.get("path") or ""))}
            elif name == "write_file":
                path = str(arguments.get("path") or "")
                sandbox.write_file(path, str(arguments.get("content") or ""))
                self._activity(thread["id"], "file", f"wrote {path}", "")
                result = {"wrote": path, "verified": False, "merged": False}
            elif name == "propose_patch":
                result = self._propose(thread, sandbox, str(arguments.get("message") or "agent patch"))
            elif name == "run_tests":
                paths = [str(item) for item in (arguments.get("paths") or [])]
                result = self._run_tests(thread, sandbox, paths)
            else:
                result = {"error": f"unknown tool {name}"}
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        self.publish(thread["id"], {"type": "activity", "kind": name or "tool", "summary": json.dumps(result, default=str)[:500]})
        return result

    def _propose(self, thread: dict[str, Any], sandbox: "Sandbox", message: str) -> dict[str, Any]:
        commit = sandbox.commit(message)
        touched = [path for path in commit["files"] if is_relative_test_path(path)]
        change_id = thread.get("change_id") or f"chat-{thread['id']}"
        evaluation = self.ws.register_agent_change(
            change_id=change_id,
            title=f"Agent patch {thread['branch']}",
            ref=thread["branch"],
            sha=commit["sha"],
            required_files=touched,
        )
        self.ws.store.execute(
            "UPDATE chat_threads SET change_id=?, base_sha=? WHERE id=?",
            (change_id, commit["sha"], thread["id"]),
        )
        thread["change_id"] = change_id
        thread["base_sha"] = commit["sha"]
        self._activity(thread["id"], "patch", commit["sha"], json.dumps(commit["files"]))
        return {
            "sha": commit["sha"],
            "files": commit["files"],
            "change_id": change_id,
            "chips": evaluation["chips"],
            "can_verify": evaluation["can_verify"],
        }

    def _run_tests(self, thread: dict[str, Any], sandbox: "Sandbox", paths: list[str]) -> dict[str, Any]:
        if sandbox.dirty():
            return {"error": "worktree has uncommitted edits; call propose_patch before run_tests"}
        if not paths:
            return {"error": "paths is required"}
        try:
            paths = sandbox.resolve_test_paths(paths)
        except ValueError as exc:
            return {"error": str(exc)}
        outcome = sandbox.pytest(paths, self._procs, thread["id"], self._flags.setdefault(thread["id"], threading.Event()))
        self._activity(thread["id"], "test", f"exit {outcome['exit_code']}", outcome["log"][-4000:])
        for line in outcome["log"].splitlines():
            self.publish(thread["id"], {"type": "log", "line": line + "\n"})
        change_id = thread.get("change_id") or ""
        acceptance = False
        if change_id:
            change = self.ws.store.query_one("SELECT * FROM changes WHERE id=?", (change_id,))
            required = json.loads(change["required_files"] or "[]") if change else []
            acceptance = bool(required) and set(required) <= set(paths) and outcome["ok"]
            if not required and outcome["ok"]:
                required = [path for path in paths if is_relative_test_path(path)]
                if required:
                    self.ws.register_agent_change(
                        change_id=change_id,
                        title=f"Agent tests {thread['branch']}",
                        ref=thread["branch"],
                        sha=outcome["sha"],
                        required_files=required,
                    )
                    acceptance = set(required) <= set(paths)
        elif outcome["ok"]:
            change_id = f"chat-{thread['id']}"
            required = [path for path in paths if is_relative_test_path(path)]
            if required:
                self.ws.register_agent_change(
                    change_id=change_id,
                    title=f"Agent tests {thread['branch']}",
                    ref=thread["branch"],
                    sha=outcome["sha"],
                    required_files=required,
                )
                self.ws.store.execute("UPDATE chat_threads SET change_id=? WHERE id=?", (change_id, thread["id"]))
                thread["change_id"] = change_id
                acceptance = True
        round_id = ""
        if change_id:
            round_id = self.ws.record_observed_round(
                title=f"agent tests {thread['id']}",
                ref=thread["branch"],
                sha=outcome["sha"],
                change_id=change_id,
                files=outcome["files"],
                log_text=outcome["log"],
                acceptance=acceptance and outcome["ok"],
            )
        return {
            "exit_code": outcome["exit_code"],
            "ok": outcome["ok"],
            "round_id": round_id,
            "change_id": change_id,
            "verified": False,
            "merged": False,
        }

    def _sandbox(self, thread: dict[str, Any]) -> "Sandbox":
        root = Path(thread["worktree"])
        if not (root / ".git").exists():
            gitrepo.add_worktree_branch(self.ws.repo, root, thread["base_sha"], thread["branch"])
        return Sandbox(root)

    def _model_messages(self, thread_id: str) -> list[dict[str, str]]:
        rows = self.ws.store.query(
            "SELECT role, content FROM chat_messages WHERE thread_id=? AND role IN ('user', 'assistant', 'tool') ORDER BY id",
            (thread_id,),
        )
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def _latest_user(self, thread_id: str) -> str:
        row = self.ws.store.query_one(
            "SELECT content FROM chat_messages WHERE thread_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (thread_id,),
        )
        return row["content"] if row else ""

    def _assistant(self, thread_id: str, content: str) -> None:
        if not content:
            return
        self.ws.store.execute(
            "INSERT INTO chat_messages (thread_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (thread_id, content, utc_now()),
        )
        self.publish(thread_id, {"type": "message", "role": "assistant", "content": content})

    def _activity(self, thread_id: str, kind: str, summary: str, detail: str) -> None:
        self.ws.store.execute(
            "INSERT INTO chat_activities (thread_id, ts, kind, summary, detail) VALUES (?, ?, ?, ?, ?)",
            (thread_id, utc_now(), kind, summary[:300], detail[:20000]),
        )
        self.ws.store.event("chat_activity", {"thread_id": thread_id, "kind": kind, "summary": summary[:300]})

    def _ensure_local_agent(self) -> None:
        if self.ws.store.query_one("SELECT id FROM chat_agents LIMIT 1"):
            return
        self.ws.store.execute(
            "INSERT INTO chat_agents (id, name, backend, model, created_at) VALUES ('agt-local', 'Local Ollama', 'ollama', '', ?)",
            (utc_now(),),
        )


class Sandbox:
    def __init__(self, root: Path):
        self.root = root

    def list_files(self, raw: str) -> dict[str, Any]:
        base = self.root if raw in {"", "."} else resolve_inside(self.root, raw)
        if not base.exists():
            raise ValueError("path does not exist")
        found = []
        if base.is_file():
            return {"files": [str(base.relative_to(self.root))]}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name != ".git"]
            for name in filenames:
                path = Path(dirpath) / name
                found.append(str(path.relative_to(self.root)))
                if len(found) >= 200:
                    return {"files": found, "truncated": True}
        return {"files": found, "truncated": False}

    def read_file(self, raw: str) -> str:
        path = resolve_inside(self.root, raw)
        if path.name in DENIED_NAMES:
            raise ValueError("refusing to read a secret file")
        if not path.is_file():
            raise ValueError("file does not exist")
        return path.read_text(encoding="utf-8", errors="replace")[:40000]

    def write_file(self, raw: str, content: str) -> None:
        path = resolve_inside(self.root, raw)
        if path.name in DENIED_NAMES or path.name.startswith(".env"):
            raise ValueError("refusing to write a secret file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def dirty(self) -> bool:
        completed = gitrepo.git(self.root, "status", "--porcelain", check=False)
        return bool((completed.stdout or "").strip())

    def resolve_test_paths(self, paths: list[str]) -> list[str]:
        resolved = []
        for raw in paths:
            candidate = resolve_inside(self.root, raw)
            if candidate.is_file():
                resolved.append(str(candidate.relative_to(self.root)))
                continue
            name = Path(raw).name
            matches = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [item for item in dirnames if item != ".git"]
                if name in filenames:
                    matches.append(str((Path(dirpath) / name).relative_to(self.root)))
            if len(matches) == 1:
                resolved.append(matches[0])
            else:
                raise ValueError(f"{raw} is not a single file in the worktree; candidates: {matches[:8]}")
        return resolved

    def commit(self, message: str) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("commit message is required")
        status = gitrepo.git(self.root, "status", "--porcelain", check=False)
        paths = []
        for line in (status.stdout or "").splitlines():
            raw = line[3:] if len(line) > 3 else ""
            name = raw.strip().split(" -> ")[-1]
            if not name or Path(name).name in DENIED_NAMES or Path(name).name.startswith(".env"):
                continue
            paths.append(name)
        if not paths:
            raise ValueError("nothing to commit")
        gitrepo.git(self.root, "add", "--", *paths)
        gitrepo.git(self.root, "commit", "-m", message.strip())
        sha = gitrepo.git(self.root, "rev-parse", "HEAD").stdout.strip()
        return {"sha": sha, "files": paths}

    def pytest(
        self,
        paths: list[str],
        procs: dict[str, subprocess.Popen[str]],
        thread_id: str,
        flag: threading.Event,
    ) -> dict[str, Any]:
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        env["AVRONE_LLM_MODE"] = "mock"
        env["AVRONE_RUNNER"] = "local"
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=short", "-q", *paths]
        proc = subprocess.Popen(
            command,
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        procs[thread_id] = proc
        if flag.is_set():
            try:
                os.killpg(proc.pid, 15)
            except ProcessLookupError:
                pass
        log = proc.communicate()[0] or ""
        code = int(proc.returncode or 0)
        procs.pop(thread_id, None)
        sha = gitrepo.git(self.root, "rev-parse", "HEAD").stdout.strip()
        status = "passed" if code == 0 else "failed"
        files = [
            {
                "path": path,
                "status": status,
                "stdout": log,
                "stderr": "",
                "trace": "" if code == 0 else log,
            }
            for path in paths
        ]
        return {"exit_code": code, "ok": code == 0, "log": log, "sha": sha, "files": files}
