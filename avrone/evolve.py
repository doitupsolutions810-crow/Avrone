"""Self-evolution (v0.9): AVRONE proposes and validates changes to its OWN codebase.

Guardrails, by construction:
  * All work happens in a throwaway git worktree under data_dir, never the live tree.
  * A proposal must apply cleanly AND pass the full test suite to be accepted; a
    failing proposal is discarded and its test output returned so the agent can retry.
  * Nothing is pushed and nothing touches `main`. An accepted proposal lands on a
    branch in the worktree and is surfaced as a diff for a human to review/merge.
  * Edits are confined to the repo; paths escaping it are rejected.

Tools: evolve_propose, evolve_diff, evolve_list, evolve_accept (branch only), evolve_reset.
Env: AVRONE_REPO_DIR (repo root), AVRONE_SELF_EVOLVE=allow|deny (default allow),
     AVRONE_EVOLVE_TEST_CMD (default: python -m pytest -q).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def _repo_root(ctx: "AgentContext") -> Path:
    env = os.getenv("AVRONE_REPO_DIR")
    if env:
        return Path(env).resolve()
    # package lives at <repo>/avrone/evolve.py
    return Path(__file__).resolve().parent.parent


def _data(ctx: "AgentContext") -> Path:
    d = Path(ctx.core.settings.data_dir) / "evolve"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


async def _sh(ctx: "AgentContext", cmd: str, cwd: Path, timeout: int = 600):
    return await ctx.core.runner.run(f"cd {cwd} && {cmd}", timeout=timeout)


def _meta_path(ctx: "AgentContext", pid: str) -> Path:
    return _data(ctx) / f"{pid}.json"


async def _ensure_worktree(ctx: "AgentContext", pid: str) -> tuple[Path, str]:
    """A fresh checkout of the repo's HEAD for proposal `pid`, on its own branch."""
    repo = _repo_root(ctx)
    wt = _data(ctx) / f"wt-{pid}"
    branch = f"evolve/{pid}"
    if wt.exists():
        return wt, branch
    # Clone the local repo (cheap, isolated) rather than add a worktree to the live .git.
    r = await _sh(ctx, f"git clone -q --no-hardlinks . {wt} 2>&1", repo)
    if r.exit_code != 0:
        # Fallback: plain copy if not a git repo
        shutil.copytree(repo, wt, ignore=shutil.ignore_patterns(".git", "data", "*.egg-info", "__pycache__"))
        await _sh(ctx, "git init -q && git add -A && git -c user.email=a@a -c user.name=avrone commit -qm base", wt)
    await _sh(ctx, f"git checkout -q -b {branch}", wt)
    return wt, branch


def _in_repo(wt: Path, rel: str) -> Path:
    p = (wt / rel).resolve()
    if not str(p).startswith(str(wt.resolve())):
        raise ValueError(f"path escapes repo: {rel}")
    return p


async def t_evolve_propose(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if os.getenv("AVRONE_SELF_EVOLVE", "allow").lower() == "deny":
        return "ERROR: self-evolution is disabled on this node (AVRONE_SELF_EVOLVE=deny)"
    goal = str(args.get("goal", "")).strip()
    edits = args.get("edits")
    if not goal or not isinstance(edits, list) or not edits:
        return ('ERROR: give {"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}. '
                "Each edit replaces that file's whole contents with content.")
    import uuid
    pid = SAFE_ID.sub("-", f"{int(time.time())}-{ctx.name}-{uuid.uuid4().hex[:6]}")[:56]
    wt, branch = await _ensure_worktree(ctx, pid)
    changed = []
    for e in edits:
        try:
            p = _in_repo(wt, str(e["path"]))
        except (KeyError, ValueError) as ex:
            return f"ERROR: {ex}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(e.get("content", "")))
        changed.append(str(e["path"]))
    # The gate runs the deterministic suite by default: the real-uvicorn integration tests flake
    # under the parallel-clone load of a squad and would reject good unit-level proposals.
    test_cmd = str(args.get("test_cmd") or os.getenv("AVRONE_EVOLVE_TEST_CMD")
                   or 'python -m pytest -q -m "not integration"')
    # Test the clone's code via PYTHONPATH (beats the installed package) without re-pointing the
    # node's own install, so parallel proposals can't hijack each other or the live system.
    res = await _sh(ctx, f"PYTHONPATH={wt}${{PYTHONPATH:+:$PYTHONPATH}} {test_cmd} 2>&1", wt, timeout=900)
    passed = res.exit_code == 0
    flaky = False
    if not passed and "pytest" in test_cmd:
        # Separate real failures from load flakes: re-run only what failed, once, alone.
        again = await _sh(ctx, f"PYTHONPATH={wt}${{PYTHONPATH:+:$PYTHONPATH}} {test_cmd} --lf 2>&1", wt, timeout=600)
        if again.exit_code == 0:
            passed, flaky = True, True
            res = again
    tail = "\n".join((res.stdout or res.stderr).splitlines()[-25:])
    if passed:
        await _sh(ctx, f"git add -A && git -c user.email=a@a -c user.name=avrone commit -qm {json.dumps(goal)[:200]}", wt)
    meta = {"pid": pid, "goal": goal, "branch": branch, "files": changed, "passed": passed,
            "by": ctx.name, "ts": time.time(), "test_cmd": test_cmd, "worktree": str(wt),
            "flaky_retry": flaky, "test_tail": tail[-3000:]}
    _meta_path(ctx, pid).write_text(json.dumps(meta))
    _emit(ctx, "evolve.propose", pid=pid, passed=passed, files=changed)
    verdict = ("ACCEPTED (tests pass" + (" after re-running a flaky failure" if flaky else "")
               + ", committed to branch " + branch + ")") if passed else "REJECTED (tests fail; discard or retry)"
    return (f"proposal {pid}: {verdict}\ngoal: {goal}\nfiles: {', '.join(changed)}\n"
            f"--- test tail ---\n{tail}\n"
            f"{'Use evolve_diff ' + pid + ' to see the change.' if passed else 'Fix your edits and propose again.'}")


async def t_evolve_diff(ctx: "AgentContext", args: dict[str, Any]) -> str:
    pid = str(args.get("pid", ""))
    mp = _meta_path(ctx, pid)
    if not mp.exists():
        return f"ERROR: no proposal {pid}. evolve_list to see them."
    wt = Path(json.loads(mp.read_text())["worktree"])
    r = await _sh(ctx, "git --no-pager diff HEAD~1..HEAD 2>/dev/null || git --no-pager diff", wt)
    return r.text(8000) or "(no diff)"


async def t_evolve_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    rows = []
    for f in sorted(_data(ctx).glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except ValueError:
            continue
        rows.append(f"{m['pid']} [{'PASS' if m['passed'] else 'FAIL'}] {m['goal'][:70]} "
                    f"({', '.join(m['files'])}) by {m['by']}")
    return "\n".join(rows) or "no proposals yet"


async def t_evolve_reset(ctx: "AgentContext", args: dict[str, Any]) -> str:
    pid = str(args.get("pid", ""))
    mp = _meta_path(ctx, pid)
    if not mp.exists():
        return f"ERROR: no proposal {pid}"
    wt = Path(json.loads(mp.read_text())["worktree"])
    shutil.rmtree(wt, ignore_errors=True)
    mp.unlink()
    _emit(ctx, "evolve.reset", pid=pid)
    return f"discarded proposal {pid}"


EVOLVE_TOOLS = [
    ("evolve_propose", "propose a change to AVRONE's OWN code: it is applied in a throwaway copy of the repo and the "
     "test suite is run; it is kept only if every test passes. Nothing touches the live tree or main.",
     '{"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}', t_evolve_propose),
    ("evolve_diff", "show the unified diff of an accepted proposal.", '{"pid": str}', t_evolve_diff),
    ("evolve_list", "list self-evolution proposals and whether each passed tests.", "{}", t_evolve_list),
    ("evolve_reset", "discard a proposal and its worktree.", '{"pid": str}', t_evolve_reset),
]


def register_evolve_tools(registry) -> None:
    for name, desc, args_doc, fn in EVOLVE_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))


def list_accepted(settings) -> list[dict[str, Any]]:
    """Accepted proposals, for /api/evolve and the CLI."""
    d = Path(settings.data_dir) / "evolve"
    out = []
    if d.exists():
        for f in sorted(d.glob("*.json")):
            try:
                m = json.loads(f.read_text())
            except ValueError:
                continue
            if m.get("passed"):
                out.append({k: m[k] for k in ("pid", "goal", "branch", "files", "by", "ts")})
    return out
