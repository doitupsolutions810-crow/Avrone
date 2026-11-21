"""Execute a round against a detached git worktree, or call Ollama for advice.

Python runs have no harness timeout. The only early stop is the operator cancel
flag (or a test-only stop_after checkpoint).
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from . import gitrepo
from .ollama_client import generate, ollama_host, probe_ollama
from .util import clip, sha256_bytes, sha256_text, utc_now

Publish = Callable[[str, dict[str, Any]], None]
FAILING = {"failed", "error"}


class RunnerBusy(RuntimeError):
    pass


class Runner:
    def __init__(self, store: Any, repo: Path, worktree_root: Path, publish: Publish | None = None):
        self.store = store
        self.repo = Path(repo)
        self.worktree_root = Path(worktree_root)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.publish = publish or (lambda _round_id, _event: None)
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._flags: dict[str, threading.Event] = {}
        self._guard = threading.Lock()
        self._active: str | None = None

    def flag(self, round_id: str) -> threading.Event:
        with self._guard:
            event = self._flags.get(round_id)
            if event is None:
                event = threading.Event()
                self._flags[round_id] = event
            return event

    def recover_orphans(self) -> list[str]:
        rows = self.store.query("SELECT id FROM rounds WHERE status='running'")
        recovered = []
        for row in rows:
            round_id = row["id"]
            self.store.execute("UPDATE rounds SET status='interrupted', finished_at=? WHERE id=?", (utc_now(), round_id))
            self._checkpoint(round_id, "server restarted while the round was running")
            self._anomaly(round_id, "server restarted while the round was running", "")
            self.store.event("round_interrupted", {"round_id": round_id, "reason": "server restarted"})
            recovered.append(round_id)
        return recovered

    def cancel(self, round_id: str) -> None:
        flag = self.flag(round_id)
        flag.set()
        proc = self._procs.get(round_id)
        self._anomaly(round_id, "operator cancelled the round; no harness timeout fired", "")
        self.store.event("round_cancel_requested", {"round_id": round_id})
        if proc is None or proc.poll() is not None:
            return

        def _grace() -> None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        threading.Thread(target=_grace, name=f"cancel-{round_id}", daemon=True).start()

    def execute(self, round_id: str, stop_after: int | None = None) -> None:
        with self._guard:
            if self._active and self._active != round_id:
                raise RunnerBusy(self._active)
            self._active = round_id
        try:
            self._execute(round_id, stop_after)
        finally:
            with self._guard:
                if self._active == round_id:
                    self._active = None

    def _execute(self, round_id: str, stop_after: int | None) -> None:
        rnd = self._round(round_id)
        if rnd["sealed"]:
            raise RuntimeError("round is sealed")
        log_dir = self._log_dir(round_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        combined = log_dir / "combined.log"
        resuming = rnd["status"] in {"interrupted", "cancelled"}
        if resuming:
            self.flag(round_id).clear()
        elif self.flag(round_id).is_set():
            self._mark_round(round_id, "cancelled", combined, note="cancelled before the round started")
            return
        now = utc_now()
        self.store.execute(
            "UPDATE rounds SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
            (now, round_id),
        )
        self.publish(round_id, {"type": "status", "status": "running"})
        self.store.event("round_started", {"round_id": round_id, "sha": rnd["commit_sha"], "runner": rnd["runner"]})
        worktree = self.worktree_root / round_id
        ran = 0
        try:
            if rnd["runner"] == "python":
                gitrepo.add_worktree(self.repo, worktree, rnd["commit_sha"])
            files = self.store.query(
                "SELECT * FROM round_files WHERE round_id=? ORDER BY ord",
                (round_id,),
            )
            for item in files:
                if item["status"] in {"passed", "failed", "error", "compiled"}:
                    continue
                if self.flag(round_id).is_set():
                    self._mark_round(round_id, "cancelled", combined)
                    return
                self._run_file(rnd, item, worktree, combined)
                ran += 1
                self._checkpoint(round_id, f"completed {item['path']}")
                if stop_after is not None and ran >= stop_after:
                    self._mark_round(round_id, "interrupted", combined, note="stopped at test checkpoint")
                    return
            if rnd["runner"] == "ollama":
                self._run_ollama(rnd, combined)
            if self.flag(round_id).is_set():
                self._mark_round(round_id, "cancelled", combined)
                return
            self._seal(round_id, combined)
        except Exception as exc:
            import traceback

            trace = traceback.format_exc()
            self._anomaly(round_id, f"round crashed: {exc}", trace)
            self._checkpoint(round_id, "crashed before the round was sealed")
            self._mark_round(round_id, "interrupted", combined, note="crash; resume from the last checkpoint")
        finally:
            self._procs.pop(round_id, None)
            if rnd["runner"] == "python":
                gitrepo.remove_worktree(self.repo, worktree)

    def _run_file(self, rnd: dict[str, Any], item: dict[str, Any], worktree: Path, combined: Path) -> None:
        round_id = rnd["id"]
        path = item["path"]
        started = utc_now()
        self.store.execute(
            "UPDATE round_files SET status='running', started_at=? WHERE round_id=? AND ord=?",
            (started, round_id, item["ord"]),
        )
        self.publish(round_id, {"type": "file", "path": path, "status": "running"})
        source, syntax_error = self._load_source(rnd, path, worktree)
        if syntax_error:
            self._finish_file(
                rnd,
                item,
                status="error",
                exit_code=1,
                stdout="",
                stderr=syntax_error,
                trace=syntax_error,
                cases=[
                    {
                        "name": "__compile__",
                        "outcome": "error",
                        "detail": "SyntaxError",
                        "trace": syntax_error,
                        "stdout": "",
                        "stderr": syntax_error,
                    }
                ],
            )
            return
        if rnd["runner"] == "ollama":
            self._finish_file(
                rnd,
                item,
                status="compiled",
                exit_code=0,
                stdout="compiled; Ollama does not execute the suite\n",
                stderr="",
                trace="",
                cases=[
                    {
                        "name": "__compile__",
                        "outcome": "compiled",
                        "detail": "file compiles; execution is not an Ollama result",
                        "trace": "",
                        "stdout": "",
                        "stderr": "",
                    }
                ],
            )
            return
        if _looks_like_pytest(source):
            self._run_pytest(rnd, item, worktree, combined)
        else:
            self._run_script(rnd, item, worktree, combined)

    def _load_source(self, rnd: dict[str, Any], path: str, worktree: Path) -> tuple[str, str | None]:
        if rnd["runner"] == "python":
            file_path = worktree / path
            if not file_path.is_file():
                return "", f"File is not in commit {rnd['commit_sha']}: {path}"
            text = file_path.read_text(encoding="utf-8", errors="replace")
        else:
            completed = gitrepo.git(self.repo, "show", f"{rnd['commit_sha']}:{path}", check=False)
            if completed.returncode != 0:
                return "", (completed.stderr or f"File is not in commit {rnd['commit_sha']}: {path}").strip()
            text = completed.stdout
        try:
            compile(text, path, "exec")
        except SyntaxError as exc:
            line = ""
            rows = text.splitlines()
            if exc.lineno and 1 <= exc.lineno <= len(rows):
                line = rows[exc.lineno - 1]
            trace = f"SyntaxError: {exc.msg} ({path}, line {exc.lineno})\n{line}\n"
            self._anomaly(rnd["id"], f"SyntaxError in {path}", trace)
            return text, trace
        return text, None

    def _run_pytest(self, rnd: dict[str, Any], item: dict[str, Any], worktree: Path, combined: Path) -> None:
        xml_path = self._log_dir(rnd["id"]) / f"{item['ord']}.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "-q",
            item["path"],
            f"--junitxml={xml_path}",
        ]
        stdout, stderr, code = self._popen(rnd["id"], command, worktree, combined, item["path"])
        if self.flag(rnd["id"]).is_set():
            self._finish_file(
                rnd,
                item,
                status="interrupted",
                exit_code=code,
                stdout=stdout,
                stderr=stderr,
                trace=stderr,
                cases=[],
            )
            return
        cases = _parse_junit(xml_path)
        failing = [case for case in cases if case["outcome"] in FAILING]
        status = "passed"
        trace = ""
        if code != 0 or failing:
            status = "failed" if any(case["outcome"] == "failed" for case in failing) else "error"
            if not cases:
                trace = _traceback_from(stderr or stdout)
                cases = [
                    {
                        "name": "__process__",
                        "outcome": "error",
                        "detail": f"exit {code}",
                        "trace": trace or (stderr or stdout),
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                ]
                self._anomaly(rnd["id"], f"process error in {item['path']} (exit {code})", trace or stderr)
            else:
                trace = "\n".join(case["trace"] for case in cases if case["outcome"] in FAILING)
        self._finish_file(rnd, item, status=status, exit_code=code, stdout=stdout, stderr=stderr, trace=trace, cases=cases)

    def _run_script(self, rnd: dict[str, Any], item: dict[str, Any], worktree: Path, combined: Path) -> None:
        stdout, stderr, code = self._popen(rnd["id"], [sys.executable, item["path"]], worktree, combined, item["path"])
        if self.flag(rnd["id"]).is_set():
            self._finish_file(
                rnd,
                item,
                status="interrupted",
                exit_code=code,
                stdout=stdout,
                stderr=stderr,
                trace=stderr,
                cases=[],
            )
            return
        cases = _script_cases(stdout + "\n" + stderr, code)
        status = "passed" if code == 0 and not any(case["outcome"] in FAILING for case in cases) else "failed"
        trace = ""
        if status != "passed":
            trace = _traceback_from(stderr or stdout) or "\n".join(case["trace"] for case in cases if case["trace"])
            if "SyntaxError" in trace or "Traceback (most recent call last)" in (stderr or ""):
                self._anomaly(rnd["id"], f"crash trace in {item['path']}", trace)
        self._finish_file(rnd, item, status=status, exit_code=code, stdout=stdout, stderr=stderr, trace=trace, cases=cases)

    def _popen(self, round_id: str, command: list[str], cwd: Path, combined: Path, label: str) -> tuple[str, str, int]:
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(cwd) + os.pathsep + env.get("PYTHONPATH", "")
        env["AVRONE_LLM_MODE"] = "mock"
        env["AVRONE_RUNNER"] = "local"
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._procs[round_id] = proc
        if self.flag(round_id).is_set():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        out_lines: list[str] = []
        err_lines: list[str] = []

        def _read(pipe: Any, sink: list[str], stream: str) -> None:
            assert pipe is not None
            for line in iter(pipe.readline, ""):
                sink.append(line)
                with combined.open("a", encoding="utf-8") as handle:
                    handle.write(f"[{label} {stream}] {line}")
                self.publish(round_id, {"type": "line", "stream": stream, "path": label, "line": line})
            pipe.close()

        threads = [
            threading.Thread(target=_read, args=(proc.stdout, out_lines, "stdout"), daemon=True),
            threading.Thread(target=_read, args=(proc.stderr, err_lines, "stderr"), daemon=True),
        ]
        for thread in threads:
            thread.start()
        # No timeout. Cancel kills the process group from another thread.
        code = proc.wait()
        for thread in threads:
            thread.join(timeout=5)
        return "".join(out_lines), "".join(err_lines), int(code)

    def _run_ollama(self, rnd: dict[str, Any], combined: Path) -> None:
        if self.store.query_one(
            "SELECT id FROM round_cases WHERE round_id=? AND file_path='__ollama__'",
            (rnd["id"],),
        ):
            return
        host = ollama_host()
        probe = probe_ollama(host)
        self.store.event("ollama_probe", {"round_id": rnd["id"], **{k: probe[k] for k in ("state", "detail", "host")}})
        if probe["state"] != "reachable":
            message = "Ollama unreachable"
            self._anomaly(rnd["id"], message, probe.get("error") or "")
            with combined.open("a", encoding="utf-8") as handle:
                handle.write(f"[ollama] {message} at {probe['host']}\n")
            self.publish(rnd["id"], {"type": "line", "stream": "stderr", "path": "__ollama__", "line": message + "\n"})
            return
        model = os.environ.get("OLLAMA_MODEL") or (probe["models"][0] if probe["models"] else "")
        if not model:
            self._anomaly(rnd["id"], "Ollama reachable but no model is installed", "")
            return
        files = self.store.query("SELECT path FROM round_files WHERE round_id=? ORDER BY ord", (rnd["id"],))
        excerpts = []
        for item in files[:3]:
            completed = gitrepo.git(self.repo, "show", f"{rnd['commit_sha']}:{item['path']}", check=False)
            text = (completed.stdout or "")[:1500]
            excerpts.append(f"### {item['path']}\n{text}")
        prompt = (
            "You are commenting on an AVRONE edge-case round. "
            "Do not claim that any test passed or failed. "
            "List concrete risks in these files.\n\n" + "\n\n".join(excerpts)
        )
        pieces: list[str] = []
        try:
            for piece in generate(host, model, prompt):
                pieces.append(piece)
                self.publish(rnd["id"], {"type": "line", "stream": "stdout", "path": "__ollama__", "line": piece})
        except Exception as exc:
            trace = f"{type(exc).__name__}: {exc}"
            self._anomaly(rnd["id"], "Ollama request failed", trace)
            return
        text = "".join(pieces)
        with combined.open("a", encoding="utf-8") as handle:
            handle.write(f"[ollama advisory model={model}]\n{text}\n")
        self.store.execute(
            """
            INSERT INTO round_cases (round_id, file_path, name, outcome, detail, trace, stdout, stderr)
            VALUES (?, '__ollama__', ?, 'advisory', ?, '', ?, '')
            """,
            (rnd["id"], model, "Ollama output is advisory and is not a test result.", text),
        )
        self.store.event("ollama_advisory", {"round_id": rnd["id"], "model": model, "sha256": sha256_text(text)})

    def _finish_file(
        self,
        rnd: dict[str, Any],
        item: dict[str, Any],
        *,
        status: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        trace: str,
        cases: list[dict[str, str]],
    ) -> None:
        log_path = self._log_dir(rnd["id"]) / f"{item['ord']}.log"
        body = f"===== {item['path']} status={status} exit={exit_code} =====\n-- stdout --\n{stdout}\n-- stderr --\n{stderr}\n-- trace --\n{trace}\n"
        log_path.write_text(body, encoding="utf-8")
        digest = sha256_bytes(body.encode("utf-8"))
        out_preview, _ = clip(stdout)
        err_preview, _ = clip(stderr)
        trace_preview, _ = clip(trace, 100_000)
        finished = utc_now()
        self.store.execute(
            """
            UPDATE round_files
               SET status=?, exit_code=?, stdout=?, stderr=?, trace=?, finished_at=?, log_sha256=?, log_path=?
             WHERE round_id=? AND ord=?
            """,
            (
                status,
                exit_code,
                out_preview,
                err_preview,
                trace_preview,
                finished,
                digest,
                str(log_path),
                rnd["id"],
                item["ord"],
            ),
        )
        for case in cases:
            self.store.execute(
                """
                INSERT INTO round_cases (round_id, file_path, name, outcome, detail, trace, stdout, stderr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rnd["id"],
                    item["path"],
                    case.get("name") or "",
                    case.get("outcome") or "",
                    case.get("detail") or "",
                    clip(case.get("trace") or "", 100_000)[0],
                    clip(case.get("stdout") or "", 50_000)[0],
                    clip(case.get("stderr") or "", 50_000)[0],
                ),
            )
        if status in FAILING:
            self._open_fixes(rnd, item["path"], cases, trace_preview or err_preview)
        elif status == "passed" and rnd.get("runner") == "python":
            self._clear_fixes(rnd, item["path"], cases)
        self.publish(rnd["id"], {"type": "file", "path": item["path"], "status": status, "sha256": digest})
        self.store.event(
            "file_finished",
            {"round_id": rnd["id"], "path": item["path"], "status": status, "sha256": digest, "at": finished},
        )

    def _open_fixes(self, rnd: dict[str, Any], path: str, cases: list[dict[str, str]], trace: str) -> None:
        failing = [case for case in cases if case.get("outcome") in FAILING] or [
            {"name": "__file__", "trace": trace, "outcome": "error"}
        ]
        for case in failing:
            name = case.get("name") or "__file__"
            identity = f"{rnd.get('change_id') or rnd['id']}|{path}|{name}"
            fix_id = "fix-" + sha256_text(identity)[:16]
            case_trace = case.get("trace") or trace
            existing = self.store.query_one("SELECT id, status FROM fixes WHERE id=?", (fix_id,))
            if existing is None:
                self.store.execute(
                    """
                    INSERT INTO fixes
                        (id, change_id, round_id, file_path, case_name, trace, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (fix_id, rnd.get("change_id"), rnd["id"], path, name, case_trace, utc_now()),
                )
                self.store.event("fix_opened", {"id": fix_id, "round_id": rnd["id"], "path": path, "case": name})
            else:
                self.store.execute(
                    """
                    UPDATE fixes
                       SET status='open', round_id=?, trace=?, reverify_round_id=NULL, cleared_at=NULL
                     WHERE id=?
                    """,
                    (rnd["id"], case_trace, fix_id),
                )
                self.store.event("fix_reopened", {"id": fix_id, "round_id": rnd["id"], "path": path, "case": name})

    def _clear_fixes(self, rnd: dict[str, Any], path: str, cases: list[dict[str, str]]) -> None:
        if not rnd.get("change_id"):
            return
        if cases and any(case.get("outcome") in FAILING for case in cases):
            return
        now = utc_now()
        rows = self.store.query(
            """
            SELECT id FROM fixes
             WHERE change_id=? AND file_path=? AND status IN ('open', 'linked')
            """,
            (rnd["change_id"], path),
        )
        for row in rows:
            self.store.execute(
                """
                UPDATE fixes
                   SET status='cleared', reverify_round_id=?, cleared_at=?
                 WHERE id=?
                """,
                (rnd["id"], now, row["id"]),
            )
            self.store.event("fix_cleared", {"id": row["id"], "round_id": rnd["id"], "path": path})

    def _seal(self, round_id: str, combined: Path) -> None:
        rnd = self._round(round_id)
        files = self.store.query("SELECT status FROM round_files WHERE round_id=?", (round_id,))
        statuses = {row["status"] for row in files}
        if statuses & FAILING:
            status = "failed"
        elif rnd["runner"] == "ollama":
            probe_failed = self.store.query_one(
                "SELECT id FROM anomalies WHERE round_id=? AND (message LIKE 'Ollama%' OR message LIKE 'Ollama unreachable%')",
                (round_id,),
            )
            status = "failed" if probe_failed else "advisory"
        elif statuses and statuses <= {"passed"}:
            status = "passed"
        else:
            status = "failed"
        self._mark_round(round_id, status, combined, seal=True)

    def _mark_round(self, round_id: str, status: str, combined: Path, *, seal: bool = False, note: str = "") -> None:
        raw = combined.read_bytes() if combined.exists() else b""
        digest = sha256_bytes(raw)
        code = 0 if status in {"passed", "advisory"} else 1
        sealed = 1 if seal or status in {"passed", "failed", "advisory"} else 0
        if status in {"interrupted", "cancelled"}:
            sealed = 0
        self.store.execute(
            """
            UPDATE rounds
               SET status=?, finished_at=?, exit_code=?, log_sha256=?, log_path=?, sealed=?
             WHERE id=?
            """,
            (status, utc_now(), code, digest, str(combined), sealed, round_id),
        )
        self._checkpoint(round_id, note or status)
        self.publish(round_id, {"type": "status", "status": status, "sha256": digest})
        self.store.event("round_status", {"round_id": round_id, "status": status, "log_sha256": digest, "sealed": sealed})

    def _checkpoint(self, round_id: str, note: str) -> None:
        files = self.store.query(
            "SELECT ord, path, status FROM round_files WHERE round_id=? ORDER BY ord",
            (round_id,),
        )
        cursor = 0
        done = []
        for item in files:
            if item["status"] in {"passed", "failed", "error", "compiled"}:
                done.append(item["path"])
                cursor = item["ord"] + 1
            else:
                break
        import json

        self.store.execute(
            """
            INSERT INTO checkpoints (round_id, ts, cursor_index, completed_json, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (round_id, utc_now(), cursor, json.dumps(done), note),
        )

    def _anomaly(self, round_id: str, message: str, trace: str) -> None:
        self.store.execute(
            "INSERT INTO anomalies (ts, round_id, message, trace) VALUES (?, ?, ?, ?)",
            (utc_now(), round_id, message, trace),
        )
        self.store.event("anomaly", {"round_id": round_id, "message": message, "at": utc_now()})
        self.publish(round_id, {"type": "anomaly", "message": message})

    def _round(self, round_id: str) -> dict[str, Any]:
        rnd = self.store.query_one("SELECT * FROM rounds WHERE id=?", (round_id,))
        if rnd is None:
            raise KeyError(round_id)
        return rnd

    def _log_dir(self, round_id: str) -> Path:
        return self.worktree_root.parent / "logs" / round_id


def _looks_like_pytest(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            return True
    return False


def _parse_junit(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    cases = []
    for node in root.iter("testcase"):
        failure = node.find("failure")
        error = node.find("error")
        skipped = node.find("skipped")
        if failure is not None:
            outcome = "failed"
            trace = ((failure.get("message") or "") + "\n" + (failure.text or "")).strip()
        elif error is not None:
            outcome = "error"
            trace = ((error.get("message") or "") + "\n" + (error.text or "")).strip()
        elif skipped is not None:
            outcome = "skipped"
            trace = ""
        else:
            outcome = "passed"
            trace = ""
        cases.append(
            {
                "name": node.get("name") or "",
                "outcome": outcome,
                "detail": node.get("classname") or "",
                "trace": trace,
                "stdout": node.findtext("system-out") or "",
                "stderr": node.findtext("system-err") or "",
            }
        )
    return cases


def _script_cases(output: str, code: int) -> list[dict[str, str]]:
    import re

    cases = []
    for line in output.splitlines():
        match = re.match(r"\[(PASS|FAIL)\]\s+(.+)$", line.strip())
        if not match:
            continue
        name = match.group(2).split(" — ")[0].strip()
        outcome = "passed" if match.group(1) == "PASS" else "failed"
        cases.append(
            {
                "name": name,
                "outcome": outcome,
                "detail": line.strip(),
                "trace": line.strip() if outcome == "failed" else "",
                "stdout": line,
                "stderr": "",
            }
        )
    if cases:
        return cases
    trace = _traceback_from(output)
    outcome = "passed" if code == 0 else "error"
    return [
        {
            "name": "__main__",
            "outcome": outcome,
            "detail": f"exit {code}",
            "trace": trace,
            "stdout": output if outcome == "passed" else "",
            "stderr": output if outcome != "passed" else "",
        }
    ]


def _traceback_from(text: str) -> str:
    marker = "Traceback (most recent call last):"
    index = text.rfind(marker)
    if index >= 0:
        return text[index:]
    if "SyntaxError:" in text:
        return text
    return ""
