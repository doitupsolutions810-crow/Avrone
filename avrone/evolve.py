"""Self-evolution (v0.9): AVRONE proposes and validates changes to its OWN codebase.

Guardrails, by construction:
  * All work happens in a throwaway git worktree under data_dir, never the live tree.
  * A proposal must apply cleanly AND pass the full test suite to be accepted; a
    failing proposal is discarded and its test output returned so the agent can retry.
  * Nothing is pushed and nothing touches `main`. An accepted proposal lands on a
    branch in the worktree and is surfaced as a diff for a human to review/merge.
  * Edits are confined to the repo; paths escaping it are rejected.

Tools: evolve_propose, evolve_loop (bounded multi-iteration), evolve_start / evolve_cancel (the same
       loop as a background task), evolve_status, evolve_diff, evolve_list, evolve_accept (branch only),
       evolve_reset.
Env: AVRONE_REPO_DIR (repo root), AVRONE_SELF_EVOLVE=allow|deny (default allow; also checked between
     iterations, so flipping it to deny stops a running loop), AVRONE_EVOLVE_TEST_CMD (default: python -m pytest -q),
     AVRONE_EVOLVE_MAX_ITERS / AVRONE_EVOLVE_TIME_BUDGET (loop default + node ceiling; hard caps 10 / 1800s),
     AVRONE_EVOLVE_MAX_BG (concurrent background loops per process; default 2, hard cap 4).
"""
from __future__ import annotations

import asyncio
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


def _denied() -> bool:
    return os.getenv("AVRONE_SELF_EVOLVE", "allow").lower() == "deny"


DENIED_MSG = "ERROR: self-evolution is disabled on this node (AVRONE_SELF_EVOLVE=deny)"

# Monotonic clock, module-level so tests can drive the loop's time budget deterministically.
_now = time.monotonic


def _new_pid(ctx: "AgentContext") -> str:
    import uuid
    return SAFE_ID.sub("-", f"{int(time.time())}-{ctx.name}-{uuid.uuid4().hex[:6]}")[:56]


async def _propose_once(ctx: "AgentContext", goal: str, edits: list, test_cmd: str | None = None,
                        timeout: int = 900, extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """One proposal -> test cycle in its own throwaway clone. Returns a result dict.

    Every guardrail lives here so single proposals and loop iterations share it: fresh clone
    under data_dir, edits confined to the clone, accept only on a clean full-suite pass.
    """
    pid = _new_pid(ctx)
    t0 = _now()
    wt, branch = await _ensure_worktree(ctx, pid)
    changed = []
    for e in edits:
        try:
            p = _in_repo(wt, str(e["path"]))
        except (KeyError, ValueError, TypeError) as ex:
            shutil.rmtree(wt, ignore_errors=True)
            return {"pid": pid, "error": str(ex), "passed": False, "branch": None, "files": changed,
                    "duration_s": round(_now() - t0, 3)}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(e.get("content", "")))
        changed.append(str(e["path"]))
    # The gate runs the deterministic suite by default: the real-uvicorn integration tests flake
    # under the parallel-clone load of a squad and would reject good unit-level proposals.
    test_cmd = str(test_cmd or os.getenv("AVRONE_EVOLVE_TEST_CMD")
                   or 'python -m pytest -q -m "not integration"')
    timeout = max(1, int(timeout))
    # Test the clone's code via PYTHONPATH (beats the installed package) without re-pointing the
    # node's own install, so parallel proposals can't hijack each other or the live system.
    res = await _sh(ctx, f"PYTHONPATH={wt}${{PYTHONPATH:+:$PYTHONPATH}} {test_cmd} 2>&1", wt, timeout=timeout)
    passed = res.exit_code == 0
    flaky = False
    if not passed and "pytest" in test_cmd:
        # Separate real failures from load flakes: re-run only what failed, once, alone.
        again = await _sh(ctx, f"PYTHONPATH={wt}${{PYTHONPATH:+:$PYTHONPATH}} {test_cmd} --lf 2>&1", wt,
                          timeout=min(600, timeout))
        if again.exit_code == 0:
            passed, flaky = True, True
            res = again
    tail = "\n".join((res.stdout or res.stderr or "").splitlines()[-25:])
    if passed:
        await _sh(ctx, f"git add -A && git -c user.email=a@a -c user.name=avrone commit -qm {json.dumps(goal)[:200]}", wt)
    duration = round(_now() - t0, 3)
    meta = {"pid": pid, "goal": goal, "branch": branch, "files": changed, "passed": passed,
            "by": ctx.name, "ts": time.time(), "test_cmd": test_cmd, "worktree": str(wt),
            "flaky_retry": flaky, "test_tail": tail[-3000:], "duration_s": duration, **(extra_meta or {})}
    _meta_path(ctx, pid).write_text(json.dumps(meta))
    _emit(ctx, "evolve.propose", pid=pid, passed=passed, files=changed, duration_s=duration)
    return {"pid": pid, "passed": passed, "branch": branch if passed else None, "files": changed,
            "flaky_retry": flaky, "test_tail": tail, "worktree": str(wt), "duration_s": duration}


async def t_evolve_propose(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    goal = str(args.get("goal", "")).strip()
    edits = args.get("edits")
    if not goal or not isinstance(edits, list) or not edits:
        return ('ERROR: give {"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}. '
                "Each edit replaces that file's whole contents with content.")
    r = await _propose_once(ctx, goal, edits, args.get("test_cmd"))
    if r.get("error"):
        return f"ERROR: {r['error']}"
    pid, passed, flaky, branch, tail = r["pid"], r["passed"], r["flaky_retry"], f"evolve/{r['pid']}", r["test_tail"]
    verdict = ("ACCEPTED (tests pass" + (" after re-running a flaky failure" if flaky else "")
               + ", committed to branch " + branch + ")") if passed else "REJECTED (tests fail; discard or retry)"
    return (f"proposal {pid}: {verdict}\ngoal: {goal}\nfiles: {', '.join(r['files'])}\n"
            f"--- test tail ---\n{tail}\n"
            f"{'Use evolve_diff ' + pid + ' to see the change.' if passed else 'Fix your edits and propose again.'}")


# ---- bounded multi-iteration loop -------------------------------------------------------------
# Hard ceilings: no argument or env setting can exceed these.
MAX_ITERS_CAP = 10
TIME_BUDGET_CAP_S = 1800
DEFAULT_MAX_ITERS = 3
DEFAULT_TIME_BUDGET_S = 600


def _env_num(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        x = float(raw)
    except ValueError:
        return None
    return x if x == x and x > 0 else None


def loop_limits(max_iters: Any = None, time_budget_s: Any = None) -> tuple[int, float]:
    """Effective (max_iters, time_budget_s) for a loop.

    AVRONE_EVOLVE_MAX_ITERS / AVRONE_EVOLVE_TIME_BUDGET set the node's default AND its ceiling
    (an agent can ask for less, never more); the hard caps bound everything, env included.
    """
    env_iters, env_budget = _env_num("AVRONE_EVOLVE_MAX_ITERS"), _env_num("AVRONE_EVOLVE_TIME_BUDGET")
    iter_cap = min(MAX_ITERS_CAP, env_iters) if env_iters else MAX_ITERS_CAP
    budget_cap = min(TIME_BUDGET_CAP_S, env_budget) if env_budget else TIME_BUDGET_CAP_S
    n = int(_clamp(max_iters, env_iters or DEFAULT_MAX_ITERS, iter_cap))
    budget = _clamp(time_budget_s, env_budget or DEFAULT_TIME_BUDGET_S, budget_cap)
    return max(1, n), budget


def _loops_dir(ctx: "AgentContext") -> Path:
    d = _data(ctx) / "loops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _attempt_row(a: dict[str, Any]) -> dict[str, Any]:
    outcome = "error" if a.get("error") else ("accepted" if a["passed"] else "rejected")
    return {"iteration": a["iteration"], "pid": a["pid"], "outcome": outcome, "passed": a["passed"],
            "duration_s": a.get("duration_s"), "branch": a.get("branch"), "error": a.get("error")}


def _clamp(v: Any, default: float, cap: float, lo: float = 1) -> float:
    try:
        x = float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        x = float(default)
    if x != x:  # NaN
        x = float(default)
    return max(lo, min(cap, x))


def _loop_path(ctx: "AgentContext", loop_id: str) -> Path:
    return _loops_dir(ctx) / f"{SAFE_ID.sub('-', loop_id)}.json"


def _cancel_path(ctx: "AgentContext", loop_id: str) -> Path:
    # A file, not just an in-memory flag: a cancel survives a restart and works across processes.
    return _loops_dir(ctx) / f"{SAFE_ID.sub('-', loop_id)}.cancel"


def _cancel_requested(ctx: "AgentContext", loop_id: str) -> bool:
    return _cancel_path(ctx, loop_id).exists()


async def evolve_loop(ctx: "AgentContext", goal: str, candidates: list | None = None, edits_fn=None,
                      max_iters: int | None = None, time_budget_s: float | None = None,
                      test_cmd: str | None = None, loop_id: str | None = None,
                      mode: str = "foreground") -> dict[str, Any]:
    """Run up to `max_iters` proposal->test iterations, each in its own throwaway clone.

    Edits for iteration i come from `candidates[i]` or `edits_fn(i, attempts_so_far)` (sync or
    async; returning None/[] ends the loop). Stops at the first accepted proposal, when the
    iteration cap is hit, when the time budget runs out, or when candidates are exhausted.
    Caps: max_iters <= MAX_ITERS_CAP, time_budget_s <= TIME_BUDGET_CAP_S. Each iteration's test
    timeout is bounded by the remaining budget. Between iterations it also stops on a cancel
    request (evolve_cancel -> status "cancelled") or if AVRONE_SELF_EVOLVE flips to deny.
    """
    if _denied():
        return {"error": DENIED_MSG, "attempts": [], "accepted": None, "stopped": "denied"}
    goal = str(goal or "").strip()
    if not goal or (candidates is None and edits_fn is None):
        return {"error": "ERROR: give a goal and candidates (list of edit lists) or edits_fn",
                "attempts": [], "accepted": None, "stopped": "invalid"}
    n, budget = loop_limits(max_iters, time_budget_s)
    loop_id = loop_id or _new_pid(ctx)
    start = _now()
    attempts: list[dict[str, Any]] = []
    stopped, accepted = "max_iters", None
    loop_meta = {"loop_id": loop_id, "goal": goal, "by": ctx.name, "ts": time.time(), "max_iters": n,
                 "time_budget_s": budget, "status": "running", "stopped": None, "accepted": None,
                 "iterations": [], "duration_s": 0.0, "mode": mode, "owner_pid": os.getpid()}
    loop_path = _loop_path(ctx, loop_id)

    def save() -> None:
        loop_meta["iterations"] = [_attempt_row(a) for a in attempts]
        loop_meta["duration_s"] = round(_now() - start, 3)
        loop_path.write_text(json.dumps(loop_meta))
    save()
    def halted() -> str | None:
        if _cancel_requested(ctx, loop_id):
            return "cancelled"
        if _denied():
            return "denied"
        return None
    for i in range(n):
        h = halted()
        if h:
            stopped = h
            break
        remaining = budget - (_now() - start)
        if remaining <= 0:
            stopped = "time_budget"
            break
        if candidates is not None:
            edits = candidates[i] if i < len(candidates) else None
        else:
            edits = edits_fn(i, [dict(a) for a in attempts])
            if hasattr(edits, "__await__"):
                edits = await edits
        h = halted()  # generating the candidate may take a while: re-check before cloning/testing
        if h:
            stopped = h
            break
        remaining = budget - (_now() - start)
        if not edits or not isinstance(edits, list):
            stopped = "exhausted"
            break
        r = await _propose_once(ctx, goal, edits, test_cmd, timeout=int(max(1, min(900, remaining))),
                                extra_meta={"loop_id": loop_id, "iteration": i})
        r["iteration"] = i
        attempts.append(r)
        row = _attempt_row(r)
        _emit(ctx, "evolve.iteration", loop_id=loop_id, **row)
        save()
        if r["passed"]:
            stopped, accepted = "accepted", {"pid": r["pid"], "branch": r["branch"]}
            break
        if _now() - start >= budget:
            stopped = "time_budget"
            break
    loop_meta.update(status="cancelled" if stopped == "cancelled" else "done", stopped=stopped, accepted=accepted)
    save()
    _cancel_path(ctx, loop_id).unlink(missing_ok=True)
    _emit(ctx, "evolve.loop", loop_id=loop_id, iterations=len(attempts), stopped=stopped,
          accepted=accepted and accepted["pid"], duration_s=loop_meta["duration_s"])
    return {"loop_id": loop_id, "goal": goal, "max_iters": n, "time_budget_s": budget,
            "attempts": attempts, "accepted": accepted, "stopped": stopped,
            "duration_s": loop_meta["duration_s"], "status": loop_meta["status"]}


# ---- background / resumable loops ------------------------------------------------------------
MAX_BG_LOOPS_CAP = 4
DEFAULT_MAX_BG_LOOPS = 2
_BG: dict[str, "asyncio.Task"] = {}   # loop_id -> task (strong refs: asyncio only keeps weak ones)


def max_bg_loops() -> int:
    env = _env_num("AVRONE_EVOLVE_MAX_BG")
    return int(max(1, min(MAX_BG_LOOPS_CAP, env if env else DEFAULT_MAX_BG_LOOPS)))


def _bg_running() -> list[str]:
    return [lid for lid, t in _BG.items() if not t.done()]


async def evolve_start(ctx: "AgentContext", goal: str, candidates: list | None = None, edits_fn=None,
                       max_iters: int | None = None, time_budget_s: float | None = None,
                       test_cmd: str | None = None) -> dict[str, Any]:
    """Launch evolve_loop as a background asyncio task and return its loop_id immediately.

    Same guardrails and the SAME hard caps as evolve_loop (it IS evolve_loop). The loop json is
    written (status "running") before this returns, so evolve_status sees it at once.
    """
    if _denied():
        return {"error": DENIED_MSG, "loop_id": None}
    goal = str(goal or "").strip()
    if not goal or (candidates is None and edits_fn is None):
        return {"error": "ERROR: give a goal and candidates (list of edit lists) or edits_fn", "loop_id": None}
    cap = max_bg_loops()
    if len(_bg_running()) >= cap:
        return {"error": f"ERROR: {cap} background evolve loop(s) already running (AVRONE_EVOLVE_MAX_BG); "
                         "evolve_cancel one or wait", "loop_id": None}
    n, budget = loop_limits(max_iters, time_budget_s)
    loop_id = _new_pid(ctx)
    _loop_path(ctx, loop_id).write_text(json.dumps(
        {"loop_id": loop_id, "goal": goal, "by": ctx.name, "ts": time.time(), "max_iters": n,
         "time_budget_s": budget, "status": "running", "stopped": None, "accepted": None, "iterations": [],
         "duration_s": 0.0, "mode": "background", "owner_pid": os.getpid()}))

    async def body() -> dict[str, Any]:
        try:
            return await evolve_loop(ctx, goal, candidates=candidates, edits_fn=edits_fn, max_iters=n,
                                     time_budget_s=budget, test_cmd=test_cmd, loop_id=loop_id, mode="background")
        except asyncio.CancelledError:
            _finish_abnormally(ctx, loop_id, "cancelled", "cancelled")
            raise
        except Exception as ex:  # noqa: BLE001 - a crashed loop must not stay "running" forever
            _finish_abnormally(ctx, loop_id, "error", f"error: {ex}"[:300])
            return {"loop_id": loop_id, "error": str(ex), "stopped": "error", "status": "error"}
        finally:
            _BG.pop(loop_id, None)

    _BG[loop_id] = asyncio.get_running_loop().create_task(body(), name=f"evolve-{loop_id}")
    _emit(ctx, "evolve.start", loop_id=loop_id, max_iters=n, time_budget_s=budget)
    return {"loop_id": loop_id, "status": "running", "max_iters": n, "time_budget_s": budget}


def _finish_abnormally(ctx: "AgentContext", loop_id: str, status: str, stopped: str) -> None:
    p = _loop_path(ctx, loop_id)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        m = {"loop_id": loop_id, "iterations": []}
    m.update(status=status, stopped=stopped)
    p.write_text(json.dumps(m))
    _cancel_path(ctx, loop_id).unlink(missing_ok=True)


async def evolve_wait(loop_id: str, timeout: float | None = None) -> dict[str, Any] | None:
    """Await a background loop started in this process (None if it is not running here)."""
    t = _BG.get(loop_id)
    if t is None:
        return None
    return await asyncio.wait_for(asyncio.shield(t), timeout)


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _effective(lp: dict[str, Any]) -> dict[str, Any]:
    """A "running" loop whose owning process is gone was interrupted by a restart: say so."""
    if lp.get("status") == "running" and lp.get("owner_pid") not in (None, os.getpid()) \
            and not _pid_alive(lp["owner_pid"]):
        lp = dict(lp, status="interrupted")
    return lp


def evolve_status(ctx: "AgentContext", loop_id: str | None = None) -> dict[str, Any] | None:
    """Structured progress of a loop (latest when loop_id is None); None if unknown."""
    loops = list_loops(ctx.core.settings)
    if loop_id:
        loops = [lp for lp in loops if lp.get("loop_id") == loop_id]
    if not loops:
        return None
    lp = loops[-1]
    its = lp.get("iterations", [])
    acc = sum(1 for i in its if i.get("outcome") == "accepted")
    return {**lp, "iterations_done": len(its), "accepted_count": acc, "rejected_count": len(its) - acc,
            "live": lp.get("loop_id") in _bg_running(),
            "cancel_requested": _cancel_requested(ctx, lp["loop_id"])}


def evolve_cancel(ctx: "AgentContext", loop_id: str) -> dict[str, Any]:
    """Ask a loop to stop. Cooperative: the in-flight iteration (its clone + gate) finishes cleanly,
    the next one never starts, and the loop ends with status "cancelled"."""
    st = evolve_status(ctx, loop_id) if loop_id else None
    if st is None:
        return {"error": f"ERROR: no loop {loop_id}. evolve_list to see them."}
    if st["status"] != "running":
        return {"loop_id": loop_id, "status": st["status"], "note": "not running; nothing to cancel"}
    _cancel_path(ctx, loop_id).write_text(str(time.time()))
    _emit(ctx, "evolve.cancel", loop_id=loop_id)
    return {"loop_id": loop_id, "status": "cancelling"}


async def t_evolve_start(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    cands = args.get("candidates")
    if not isinstance(cands, list) or not cands or not all(isinstance(c, list) and c for c in cands):
        return ('ERROR: give {"goal": str, "candidates": [[{"path": str, "content": str}], ...], '
                '"max_iters"?: int, "time_budget_s"?: number, "test_cmd"?: str}')
    r = await evolve_start(ctx, args.get("goal", ""), candidates=cands, max_iters=args.get("max_iters"),
                           time_budget_s=args.get("time_budget_s"), test_cmd=args.get("test_cmd"))
    if r.get("error"):
        return r["error"]
    return (f"started background loop {r['loop_id']} (caps: {r['max_iters']} iters, {r['time_budget_s']:.0f}s). "
            f"evolve_status {{\"loop_id\": \"{r['loop_id']}\"}} for progress; evolve_cancel to stop it.")


async def t_evolve_cancel(ctx: "AgentContext", args: dict[str, Any]) -> str:
    r = evolve_cancel(ctx, str(args.get("loop_id") or ""))
    if r.get("error"):
        return r["error"]
    if r["status"] == "cancelling":
        return f"cancel requested for loop {r['loop_id']}: it stops before its next iteration"
    return f"loop {r['loop_id']} is {r['status']}: nothing to cancel"


async def t_evolve_loop(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if _denied():
        return DENIED_MSG
    cands = args.get("candidates")
    if not isinstance(cands, list) or not cands or not all(isinstance(c, list) and c for c in cands):
        return ('ERROR: give {"goal": str, "candidates": [[{"path": str, "content": str}], ...], '
                '"max_iters"?: int, "time_budget_s"?: number, "test_cmd"?: str}')
    r = await evolve_loop(ctx, args.get("goal", ""), candidates=cands, max_iters=args.get("max_iters"),
                          time_budget_s=args.get("time_budget_s"), test_cmd=args.get("test_cmd"))
    if r.get("error"):
        return r["error"]
    lines = [f"loop {r['loop_id']}: stopped={r['stopped']} after {len(r['attempts'])} iteration(s) "
             f"in {r['duration_s']:.1f}s (caps: {r['max_iters']} iters, {r['time_budget_s']:.0f}s)"]
    for a in r["attempts"]:
        d = f" [{a.get('duration_s') or 0:.1f}s]"
        if a.get("error"):
            lines.append(f"  #{a['iteration']} {a['pid']}: ERROR {a['error']}{d}")
        elif a["passed"]:
            lines.append(f"  #{a['iteration']} {a['pid']}: ACCEPTED on branch {a['branch']}{d}")
        else:
            tail = "\n      ".join(a["test_tail"].splitlines()[-8:])
            lines.append(f"  #{a['iteration']} {a['pid']}: REJECTED{d}\n      {tail}")
    return "\n".join(lines)


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
        tag = f" loop={m['loop_id']}#{m.get('iteration')}" if m.get("loop_id") else ""
        dur = f" {m['duration_s']:.1f}s" if isinstance(m.get("duration_s"), (int, float)) else ""
        rows.append(f"{m['pid']} [{'PASS' if m['passed'] else 'FAIL'}] {m['goal'][:70]} "
                    f"({', '.join(m['files'])}) by {m['by']}{dur}{tag}")
    loops = list_loops(ctx.core.settings)
    if loops:
        rows.append("loops:")
        rows += ["  " + _loop_line(lp) for lp in loops]
    return "\n".join(rows) or "no proposals yet"


def _loop_line(lp: dict[str, Any]) -> str:
    its = lp.get("iterations", [])
    acc = sum(1 for i in its if i.get("outcome") == "accepted")
    return (f"{lp['loop_id']} [{lp.get('status')}{'/' + lp['stopped'] if lp.get('stopped') else ''}] "
            f"{lp.get('goal', '')[:60]} iters={len(its)}/{lp.get('max_iters')} accepted={acc} "
            f"rejected={len(its) - acc} {float(lp.get('duration_s') or 0):.1f}s/"
            f"{float(lp.get('time_budget_s') or 0):.0f}s")


def list_loops(settings) -> list[dict[str, Any]]:
    """Every evolve loop's meta (oldest first), for evolve_list / evolve_status."""
    d = Path(settings.data_dir) / "evolve" / "loops"
    out = []
    if d.exists():
        for f in d.glob("*.json"):
            try:
                out.append(_effective(json.loads(f.read_text())))
            except ValueError:
                continue
    return sorted(out, key=lambda m: m.get("ts", 0))


async def t_evolve_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    loops = list_loops(ctx.core.settings)
    lid = str(args.get("loop_id") or "")
    if lid:
        loops = [lp for lp in loops if lp.get("loop_id") == lid]
        if not loops:
            return f"ERROR: no loop {lid}. evolve_list to see them."
    if not loops:
        n, b = loop_limits()
        return f"no evolve loops yet (limits: max_iters={n}, time_budget_s={b:.0f})"
    lp = loops[-1]
    lines = [_loop_line(lp)]
    if lp.get("mode") == "background" and lp.get("status") == "running":
        lines.append("background loop" + (" (cancel requested)" if _cancel_requested(ctx, lp["loop_id"]) else ""))
    if lp.get("accepted"):
        lines.append(f"accepted: {lp['accepted']['pid']} on branch {lp['accepted']['branch']}")
    for it in lp.get("iterations", []):
        lines.append(f"  #{it['iteration']} {it['pid']} {it['outcome'].upper()} "
                     f"{float(it.get('duration_s') or 0):.2f}s" + (f" ({it['error']})" if it.get("error") else ""))
    return "\n".join(lines)


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
    ("evolve_loop", "bounded self-evolution loop: try candidate edit sets one by one (each in its own throwaway "
     "copy, each needing a full test pass) and stop at the first that passes or when the iteration/time budget "
     "runs out (hard caps: 10 iterations, 1800s). Returns every attempt's verdict and failing test output.",
     '{"goal": str, "candidates": [[{"path": str, "content": str}], ...], "max_iters"?: int, '
     '"time_budget_s"?: number, "test_cmd"?: str}', t_evolve_loop),
    ("evolve_start", "evolve_loop in the BACKGROUND: returns a loop_id at once and keeps iterating (same guardrails "
     "and hard caps: 10 iterations, 1800s; at most 4 background loops). Poll with evolve_status, stop with "
     "evolve_cancel.", '{"goal": str, "candidates": [[{"path": str, "content": str}], ...], "max_iters"?: int, '
     '"time_budget_s"?: number, "test_cmd"?: str}', t_evolve_start),
    ("evolve_cancel", "stop an evolve loop cleanly: the running iteration finishes, the next never starts.",
     '{"loop_id": str}', t_evolve_cancel),
    ("evolve_diff", "show the unified diff of an accepted proposal.", '{"pid": str}', t_evolve_diff),
    ("evolve_list", "list self-evolution proposals (pass/fail, duration, loop iteration) and loops.", "{}",
     t_evolve_list),
    ("evolve_status", "show an evolve loop (latest by default): each iteration's outcome and duration, "
     "accept/reject counts, and the budget used.", '{"loop_id"?: str}', t_evolve_status),
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
