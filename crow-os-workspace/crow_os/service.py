"""Workspace operations shared by the HTTP API and the tests."""

from __future__ import annotations

import json
import platform
import sys
import threading
import uuid
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from . import gitrepo
from .compare import compare_rounds
from .db import Store
from .ingest import ingest as ingest_repo
from .ollama_client import ollama_host, probe_ollama
from .runner import Runner, RunnerBusy
from .status import (
    HUMAN_GATE_ID,
    HUMAN_GATE_NAME,
    choose_workspace_gate,
    evaluate_change,
    propagation_graph,
    verification_decision,
)
from .util import is_relative_test_path, sha256_bytes, utc_now

LANES = {
    "coverage": "coverage/tests",
    "tools": "new tools",
    "skills": "agent skills",
    "robustness": "robustness",
}
AGENT_KINDS = {"human", "cursor_cloud", "ollama"}
HONESTY = {
    "example_data": False,
    "branches": "git for-each-ref",
    "commits": "git log / branch tips",
    "tests": "git blobs, compiled locally",
    "artifacts": "SHA-256 of git blobs",
    "pulls": "GitHub API when GITHUB_TOKEN or GH_TOKEN is set",
    "checks": "GitHub check-runs when a token is set",
    "rounds": "subprocess or Ollama calls made by this server",
}


class Workspace:
    def __init__(self, store: Store, repo: Path, worktree_root: Path):
        self.store = store
        self.repo = Path(repo)
        self.runner = Runner(store, self.repo, worktree_root, publish=self._publish)
        self._threads: dict[str, threading.Thread] = {}
        self._subs: dict[str, list[Any]] = {}
        self._sub_lock = threading.Lock()

    def _publish(self, round_id: str, event: dict[str, Any]) -> None:
        import queue

        with self._sub_lock:
            subs = list(self._subs.get(round_id, []))
        for item in subs:
            if isinstance(item, queue.Queue):
                item.put(event)

    def subscribe(self, round_id: str):
        import queue

        item: queue.Queue = queue.Queue()
        with self._sub_lock:
            self._subs.setdefault(round_id, []).append(item)
        return item

    def unsubscribe(self, round_id: str, item: Any) -> None:
        with self._sub_lock:
            subs = self._subs.get(round_id, [])
            if item in subs:
                subs.remove(item)

    def ingest(self, token: str | None = None) -> dict[str, Any]:
        return ingest_repo(self.store, self.repo, token=token)

    def recover(self) -> list[str]:
        return self.runner.recover_orphans()

    def _start_thread(self, round_id: str) -> None:
        def _run() -> None:
            try:
                self.runner.execute(round_id)
            except Exception as exc:  # surfaced in the event log; the round row records the crash
                self.store.event("round_thread_error", {"round_id": round_id, "error": str(exc)})

        thread = threading.Thread(target=_run, name=round_id, daemon=True)
        self._threads[round_id] = thread
        thread.start()

    def overview(self) -> dict[str, Any]:
        ingest_meta = self.store.meta_get("ingest") or {}
        evaluations = self._evaluations()
        counts = {
            "branches": self.store.query_one("SELECT COUNT(*) AS n FROM branches")["n"],
            "commits": self.store.query_one("SELECT COUNT(*) AS n FROM commits")["n"],
            "pulls": self.store.query_one("SELECT COUNT(*) AS n FROM pulls")["n"],
            "checks": self.store.query_one("SELECT COUNT(*) AS n FROM checks")["n"],
            "test_files": self.store.query_one("SELECT COUNT(*) AS n FROM test_files")["n"],
            "artifacts": self.store.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"],
            "rounds": self.store.query_one("SELECT COUNT(*) AS n FROM rounds")["n"],
            "open_fixes": self.store.query_one(
                "SELECT COUNT(*) AS n FROM fixes WHERE status IN ('open', 'linked')"
            )["n"],
            "changes": len(evaluations),
            "assignments": self.store.query_one("SELECT COUNT(*) AS n FROM assignments")["n"],
        }
        try:
            head = gitrepo.head_sha(self.repo)
            branch = gitrepo.current_branch(self.repo)
            dirty = gitrepo.worktree_dirty(self.repo)
        except gitrepo.GitError:
            head, branch, dirty = "", "", False
        return {
            "repo": str(self.repo),
            "head": head,
            "branch": branch,
            "worktree_dirty": dirty,
            "github": self.store.meta_get("github")
            or {"state": "missing_token", "detail": "Ingest has not run."},
            "ollama": probe_ollama(),
            "counts": counts,
            "next_gate": choose_workspace_gate(evaluations),
            "honesty": HONESTY,
            "ingested_at": ingest_meta.get("fetched_at"),
            "warnings": ingest_meta.get("warnings") or [],
            "changes": [
                {
                    "id": item["change_id"],
                    "title": item["title"],
                    "chips": item["chips"],
                    "can_verify": item["can_verify"],
                    "next_gate": item["next_gate"],
                }
                for item in evaluations
            ],
        }

    def branches(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM branches ORDER BY is_remote, name")

    def commits(self, branch: str | None = None) -> list[dict[str, Any]]:
        if branch:
            return self.store.query(
                "SELECT * FROM commits WHERE branch=? ORDER BY committed_at DESC",
                (branch,),
            )
        return self.store.query("SELECT * FROM commits ORDER BY committed_at DESC LIMIT 300")

    def pulls(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM pulls ORDER BY number DESC")
        by_pr = {item["change_id"]: item for item in self._evaluations() if str(item["change_id"]).startswith("pr-")}
        for row in rows:
            evaluation = by_pr.get(f"pr-{row['number']}")
            row["chips"] = evaluation["chips"] if evaluation else ["unverified"]
            row["can_verify"] = bool(evaluation and evaluation["can_verify"])
        return rows

    def checks(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM checks ORDER BY name")

    def suites(self, ref: str | None = None, suite: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM test_files"
        args: list[Any] = []
        where = []
        if ref:
            where.append("ref=?")
            args.append(ref)
        if suite:
            where.append("suite=?")
            args.append(suite)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ref, path"
        rows = self.store.query(sql, tuple(args))
        for row in rows:
            row["components"] = json.loads(row.get("components") or "[]")
            row["syntax_ok"] = bool(row["syntax_ok"])
        return rows

    def artifacts(self, ref: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM artifacts"
        args: list[Any] = []
        where = []
        if ref:
            where.append("ref=?")
            args.append(ref)
        if kind:
            where.append("kind=?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ref, path"
        return self.store.query(sql, tuple(args))

    def artifact_text(self, ref: str, path: str) -> dict[str, Any]:
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise ValueError("path must stay inside the repository")
        try:
            sha = gitrepo.rev_parse(self.repo, ref)
        except gitrepo.GitError as exc:
            raise ValueError(str(exc)) from exc
        completed = gitrepo.git(self.repo, "cat-file", "-e", f"{sha}:{path}", check=False)
        if completed.returncode != 0:
            raise KeyError(path)
        blobs = gitrepo.cat_blobs(self.repo, [gitrepo.git(self.repo, "rev-parse", f"{sha}:{path}").stdout.strip()])
        blob = next(iter(blobs.values()), b"")
        digest = sha256_bytes(blob)
        try:
            text = blob.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            text = ""
            binary = True
        preview = text[:8000]
        return {
            "ref": ref,
            "sha": sha,
            "path": path,
            "sha256": digest,
            "size": len(blob),
            "binary": binary,
            "truncated": len(text) > 8000,
            "text": preview,
        }

    def changes(self) -> list[dict[str, Any]]:
        return self._evaluations()

    def create_change(self, *, title: str, ref: str, required_files: list[str]) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("title is required")
        sha = gitrepo.rev_parse(self.repo, ref)
        files = []
        for path in required_files:
            if not is_relative_test_path(path):
                raise ValueError(f"not a test path: {path}")
            probe = gitrepo.git(self.repo, "cat-file", "-e", f"{sha}:{path}", check=False)
            if probe.returncode != 0:
                raise ValueError(f"{path} is not in {sha}")
            files.append(path)
        if not files:
            raise ValueError("a change needs at least one acceptance file")
        change_id = "change-" + uuid.uuid4().hex[:12]
        main = self._main_sha()
        contained = gitrepo.is_ancestor(self.repo, sha, main) if main else False
        self.store.execute(
            """
            INSERT INTO changes
                (id, title, ref, sha, pr_number, source, required_source, required_files, contained_in_main, created_at)
            VALUES (?, ?, ?, ?, NULL, 'manual', 'manual', ?, ?, ?)
            """,
            (change_id, title.strip(), ref, sha, json.dumps(files), 1 if contained else 0, utc_now()),
        )
        self.store.execute(
            """
            INSERT INTO gates (change_id, gate_id, name, kind, passed, detail)
            VALUES (?, ?, ?, 'human', 0, ?)
            """,
            (
                change_id,
                HUMAN_GATE_ID,
                HUMAN_GATE_NAME,
                "Waiting for an operator to accept the recorded evidence.",
            ),
        )
        self.store.event("change_created", {"id": change_id, "ref": ref, "sha": sha, "files": files})
        return self.evaluation(change_id)

    def attest_gate(self, change_id: str, gate_id: str, passed: bool) -> dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM gates WHERE change_id=? AND gate_id=?",
            (change_id, gate_id),
        )
        if row is None or row["kind"] != "human":
            raise PermissionError("automatic gates are computed from runs and cannot be toggled")
        detail = (
            "Operator attested the evidence."
            if passed
            else "Waiting for an operator to accept the recorded evidence."
        )
        self.store.execute(
            "UPDATE gates SET passed=?, detail=? WHERE change_id=? AND gate_id=?",
            (1 if passed else 0, detail, change_id, gate_id),
        )
        self.store.event("gate_attested", {"change_id": change_id, "gate_id": gate_id, "passed": bool(passed)})
        return self.evaluation(change_id)

    def verify(self, change_id: str) -> dict[str, Any]:
        evaluation = self.evaluation(change_id)
        verification_decision(evaluation)
        self.store.event(
            "verification_recorded",
            {"change_id": change_id, "sha": evaluation.get("sha"), "gates": [g["gate_id"] for g in evaluation["gates"]]},
        )
        return evaluation

    def launch_round(
        self,
        *,
        title: str,
        ref: str,
        files: list[str],
        runner: str,
        change_id: str | None = None,
        acceptance: bool = False,
        background: bool = False,
    ) -> dict[str, Any]:
        if runner not in {"python", "ollama"}:
            raise ValueError("runner must be python or ollama")
        sha = gitrepo.rev_parse(self.repo, ref)
        clean_files: list[str] = []
        for path in files:
            if not is_relative_test_path(path):
                raise ValueError(f"not a test path: {path}")
            probe = gitrepo.git(self.repo, "cat-file", "-e", f"{sha}:{path}", check=False)
            if probe.returncode != 0:
                raise ValueError(f"{path} is not in {ref}")
            clean_files.append(path)
        if not clean_files:
            raise ValueError("choose at least one test file")
        if change_id and not self.store.query_one("SELECT id FROM changes WHERE id=?", (change_id,)):
            raise KeyError(change_id)
        if self.runner._active:
            raise RunnerBusy(self.runner._active)
        round_id = "r" + utc_now().replace("-", "").replace(":", "") + uuid.uuid4().hex[:6]
        inputs = {
            "title": title or f"{runner} round on {ref}",
            "ref": ref,
            "commit_sha": sha,
            "runner": runner,
            "files": clean_files,
            "acceptance": bool(acceptance),
            "change_id": change_id,
        }
        try:
            pytest_version = pkg_version("pytest")
        except Exception:
            pytest_version = ""
        env = {
            "python": sys.version,
            "platform": platform.platform(),
            "pytest": pytest_version,
            "ollama_host": ollama_host(),
            "github_token_present": bool(__import__("os").environ.get("GITHUB_TOKEN") or __import__("os").environ.get("GH_TOKEN")),
            "AVRONE_LLM_MODE": "mock",
            "AVRONE_RUNNER": "local",
            "recorded_at": utc_now(),
        }
        self.store.execute(
            """
            INSERT INTO rounds
                (id, title, ref, commit_sha, runner, status, created_at, env_json, inputs_json,
                 change_id, acceptance, sealed)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, 0)
            """,
            (
                round_id,
                inputs["title"],
                ref,
                sha,
                runner,
                utc_now(),
                json.dumps(env),
                json.dumps(inputs),
                change_id,
                1 if acceptance else 0,
            ),
        )
        self.store.executemany(
            "INSERT INTO round_files (round_id, ord, path, status) VALUES (?, ?, ?, 'pending')",
            [(round_id, index, path) for index, path in enumerate(clean_files)],
        )
        self.store.event("round_launched", {"round_id": round_id, **inputs, "env": env})
        if background:
            self._start_thread(round_id)
        return self.round_detail(round_id)

    def execute_round(self, round_id: str, stop_after: int | None = None) -> dict[str, Any]:
        self.runner.execute(round_id, stop_after=stop_after)
        return self.round_detail(round_id)

    def cancel_round(self, round_id: str) -> dict[str, Any]:
        if not self.store.query_one("SELECT id FROM rounds WHERE id=?", (round_id,)):
            raise KeyError(round_id)
        self.runner.cancel(round_id)
        return {"id": round_id, "cancel": "requested"}

    def resume_round(self, round_id: str, *, background: bool = False) -> dict[str, Any]:
        rnd = self.store.query_one("SELECT * FROM rounds WHERE id=?", (round_id,))
        if rnd is None:
            raise KeyError(round_id)
        if rnd["sealed"] or rnd["status"] not in {"interrupted", "cancelled"}:
            raise ValueError("only an interrupted or cancelled round can be resumed")
        before = rnd["inputs_json"]
        self.store.event("round_resume", {"round_id": round_id, "from_status": rnd["status"]})
        if background:
            self._start_thread(round_id)
            return self.round_detail(round_id)
        detail = self.execute_round(round_id)
        after = self.store.query_one("SELECT inputs_json, commit_sha FROM rounds WHERE id=?", (round_id,))
        if after["inputs_json"] != before or after["commit_sha"] != rnd["commit_sha"]:
            raise RuntimeError("resume mutated an immutable round input")
        return detail

    def rounds(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM rounds ORDER BY created_at DESC")
        for row in rows:
            row["chips"] = _round_chips(row)
            row["inputs"] = json.loads(row.pop("inputs_json") or "{}")
            row["env"] = json.loads(row.pop("env_json") or "{}")
        return rows

    def round_detail(self, round_id: str) -> dict[str, Any]:
        rnd = self.store.query_one("SELECT * FROM rounds WHERE id=?", (round_id,))
        if rnd is None:
            raise KeyError(round_id)
        files = self.store.query("SELECT * FROM round_files WHERE round_id=? ORDER BY ord", (round_id,))
        cases = self.store.query(
            "SELECT * FROM round_cases WHERE round_id=? ORDER BY id",
            (round_id,),
        )
        rnd["inputs"] = json.loads(rnd.pop("inputs_json") or "{}")
        rnd["env"] = json.loads(rnd.pop("env_json") or "{}")
        rnd["files"] = files
        rnd["cases"] = cases
        rnd["checkpoints"] = self.store.query(
            "SELECT * FROM checkpoints WHERE round_id=? ORDER BY id",
            (round_id,),
        )
        rnd["anomalies"] = self.store.query(
            "SELECT * FROM anomalies WHERE round_id=? ORDER BY id",
            (round_id,),
        )
        rnd["fixes"] = self.store.query("SELECT * FROM fixes WHERE round_id=? OR reverify_round_id=? ORDER BY created_at", (round_id, round_id))
        rnd["chips"] = _round_chips(rnd, files)
        if rnd.get("change_id"):
            try:
                rnd["change"] = {
                    "id": rnd["change_id"],
                    "chips": self.evaluation(rnd["change_id"])["chips"],
                    "can_verify": self.evaluation(rnd["change_id"])["can_verify"],
                }
            except KeyError:
                rnd["change"] = None
        return rnd

    def compare(self, round_a: str, round_b: str) -> dict[str, Any]:
        return compare_rounds(self.round_detail(round_a), self.round_detail(round_b))

    def fixes(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM fixes ORDER BY created_at DESC")
        for row in rows:
            row["chips"] = _fix_chips(row)
        return rows

    def link_fix(self, fix_id: str, *, commit: str | None, pr: int | None) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM fixes WHERE id=?", (fix_id,))
        if row is None:
            raise KeyError(fix_id)
        if row["status"] == "cleared":
            raise ValueError("a cleared fix stays cleared; link the new failure instead")
        sha = None
        if commit:
            sha = gitrepo.rev_parse(self.repo, commit)
        self.store.execute(
            """
            UPDATE fixes
               SET status='linked', fix_commit=COALESCE(?, fix_commit), fix_pr=COALESCE(?, fix_pr)
             WHERE id=? AND status!='cleared'
            """,
            (sha, pr, fix_id),
        )
        self.store.event("fix_linked", {"id": fix_id, "commit": sha, "pr": pr})
        updated = self.store.query_one("SELECT * FROM fixes WHERE id=?", (fix_id,))
        updated["chips"] = _fix_chips(updated)
        return updated

    def assignments(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM assignments ORDER BY created_at DESC")

    def assign(self, *, lane: str, agent_kind: str, agent_ref: str, note: str = "", change_id: str | None = None) -> dict[str, Any]:
        if lane not in LANES:
            raise ValueError(f"lane must be one of {', '.join(LANES)}")
        if agent_kind not in AGENT_KINDS:
            raise ValueError("agent_kind must be human, cursor_cloud, or ollama")
        if not agent_ref.strip():
            raise ValueError("agent_ref is required")
        if change_id and not self.store.query_one("SELECT id FROM changes WHERE id=?", (change_id,)):
            raise KeyError(change_id)
        assignment_id = "asg-" + uuid.uuid4().hex[:12]
        self.store.execute(
            """
            INSERT INTO assignments (id, lane, agent_kind, agent_ref, note, change_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (assignment_id, lane, agent_kind, agent_ref.strip(), note.strip(), change_id, utc_now()),
        )
        self.store.event(
            "assignment",
            {"id": assignment_id, "lane": lane, "agent_kind": agent_kind, "agent_ref": agent_ref.strip(), "change_id": change_id},
        )
        return self.store.query_one("SELECT * FROM assignments WHERE id=?", (assignment_id,))

    def checkpoints(self) -> list[dict[str, Any]]:
        return self.store.query(
            """
            SELECT c.*, r.title, r.ref, r.status AS round_status
              FROM checkpoints c
              JOIN rounds r ON r.id = c.round_id
             ORDER BY c.id DESC
             LIMIT 400
            """
        )

    def propagation(self) -> dict[str, Any]:
        changes = self.store.query("SELECT * FROM changes")
        syntax = []
        for change in changes:
            required = json.loads(change["required_files"] or "[]")
            if not required:
                continue
            placeholders = ",".join("?" for _ in required)
            rows = self.store.query(
                f"SELECT path, syntax_error, components FROM test_files WHERE ref=? AND syntax_ok=0 AND path IN ({placeholders})",
                (change["ref"], *required),
            )
            for row in rows:
                syntax.append(
                    {
                        "change_id": change["id"],
                        "path": row["path"],
                        "syntax_error": row["syntax_error"],
                        "components": json.loads(row["components"] or "[]"),
                    }
                )
        fixes = self.store.query("SELECT * FROM fixes")
        return propagation_graph(changes=changes, syntax_failures=syntax, fixes=fixes)

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.events(limit)

    def evaluation(self, change_id: str) -> dict[str, Any]:
        found = [item for item in self._evaluations() if item["change_id"] == change_id]
        if not found:
            raise KeyError(change_id)
        return found[0]

    def _evaluations(self) -> list[dict[str, Any]]:
        changes = self.store.query("SELECT * FROM changes ORDER BY created_at")
        github = self.store.meta_get("github") or {}
        results = []
        for change in changes:
            required = json.loads(change["required_files"] or "[]")
            syntax_rows = []
            if required:
                placeholders = ",".join("?" for _ in required)
                syntax_rows = self.store.query(
                    f"SELECT path, syntax_ok, syntax_error FROM test_files WHERE ref=? AND path IN ({placeholders})",
                    (change["ref"], *required),
                )
            file_syntax = {
                row["path"]: {"syntax_ok": bool(row["syntax_ok"]), "syntax_error": row["syntax_error"]}
                for row in syntax_rows
            }
            rounds = []
            for rnd in self.store.query("SELECT * FROM rounds WHERE change_id=?", (change["id"],)):
                rnd["files"] = self.store.query(
                    "SELECT path, status, log_sha256 FROM round_files WHERE round_id=?",
                    (rnd["id"],),
                )
                rnd["acceptance"] = bool(rnd["acceptance"])
                rounds.append(rnd)
            fixes = self.store.query("SELECT * FROM fixes WHERE change_id=?", (change["id"],))
            human = self.store.query(
                "SELECT gate_id, name, passed, detail FROM gates WHERE change_id=? AND kind='human'",
                (change["id"],),
            )
            for gate in human:
                gate["passed"] = bool(gate["passed"])
            pull = None
            checks = None
            if change.get("pr_number"):
                pull_row = self.store.query_one("SELECT * FROM pulls WHERE number=?", (change["pr_number"],))
                if pull_row:
                    pull = {
                        "number": pull_row["number"],
                        "draft": bool(pull_row["draft"]),
                        "merged": bool(pull_row["merged"]),
                        "state": pull_row["state"],
                    }
                if github.get("state") == "ok":
                    checks = self.store.query("SELECT * FROM checks WHERE pr_number=?", (change["pr_number"],))
            payload = {
                "id": change["id"],
                "title": change["title"],
                "ref": change["ref"],
                "sha": change["sha"],
                "pr_number": change.get("pr_number"),
                "contained_in_main": bool(change.get("contained_in_main")),
                "source": change.get("source"),
                "required_files": required,
            }
            evaluation = evaluate_change(
                payload,
                required_files=required,
                file_syntax=file_syntax,
                rounds=rounds,
                fixes=fixes,
                human_gates=human,
                pull=pull,
                checks=checks,
            )
            evaluation["ref"] = change["ref"]
            evaluation["source"] = change["source"]
            evaluation["required_files"] = required
            evaluation["pr_number"] = change.get("pr_number")
            results.append(evaluation)
        results.sort(key=lambda item: item.get("title") or "")
        return results

    def _main_sha(self) -> str:
        for ref in ("origin/main", "main"):
            try:
                return gitrepo.rev_parse(self.repo, ref)
            except gitrepo.GitError:
                continue
        return gitrepo.head_sha(self.repo)


def _round_chips(rnd: dict[str, Any], files: list[dict[str, Any]] | None = None) -> list[str]:
    chips = ["unverified"]
    status = rnd.get("status")
    if status in {"open", "running", "interrupted"}:
        chips.append("open")
    if status == "interrupted":
        chips.append("blocked")
    file_rows = files if files is not None else []
    if status == "failed" or any(item.get("status") in {"failed", "error"} for item in file_rows):
        chips.append("failed")
    if status == "advisory":
        chips.append("draft")
    order = ["draft", "open", "merged", "unmerged", "failed", "blocked", "verified", "unverified"]
    return [chip for chip in order if chip in set(chips)]


def _fix_chips(row: dict[str, Any]) -> list[str]:
    status = row.get("status")
    chips = ["unverified", "unmerged"]
    if status == "open":
        chips.extend(["open", "failed", "blocked"])
    elif status == "linked":
        chips.extend(["open", "failed", "blocked"])
    elif status == "cleared":
        chips = ["unverified"]
    order = ["draft", "open", "merged", "unmerged", "failed", "blocked", "verified", "unverified"]
    return [chip for chip in order if chip in set(chips)]
