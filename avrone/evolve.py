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

import fcntl
import json
import os
import shlex
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,55}$")

DEFAULT_MAX_PROPOSAL_SECONDS = 900
MAX_PROPOSAL_SECONDS = 1800
MAX_PROPOSAL_EDITS = 20
MAX_EDIT_BYTES = 250_000
MAX_TOTAL_EDIT_BYTES = 1_000_000
DEFAULT_MAX_DAILY_SECONDS = 3600
HARD_MAX_DAILY_SECONDS = 14400
DEFAULT_MAX_DAILY_PROPOSALS = 12
HARD_MAX_DAILY_PROPOSALS = 100
SHELL_META = re.compile(r"[;&|\x60$><\n\r]")


def _repo_root(ctx: "AgentContext") -> Path:
    env = os.getenv("AVRONE_REPO_DIR")
    if env:
        return Path(env).resolve()
    # package lives at <repo>/avrone/evolve.py
    return Path(__file__).resolve().parent.parent


def _data(ctx: "AgentContext") -> Path:
    root = Path(ctx.core.settings.data_dir).resolve()
    d = root / "evolve"
    if d.is_symlink():
        raise ValueError("evolution data directory must not be a symlink")
    d.mkdir(parents=True, exist_ok=True)
    resolved = d.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("evolution data directory escapes the configured data root") from None
    return resolved


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


async def _sh(ctx: "AgentContext", cmd: str, cwd: Path, timeout: int = 600):
    """Run a command with a shell-quoted working directory and explicit timeout."""
    return await ctx.core.runner.run(f"cd {shlex.quote(str(cwd))} && {cmd}", timeout=timeout)


def _proposal_timeout(args: dict[str, Any]) -> int:
    """Return requested duration, bounded by the operator-configured ceiling."""
    try:
        configured = int(os.getenv("AVRONE_EVOLVE_MAX_SECONDS", str(DEFAULT_MAX_PROPOSAL_SECONDS)))
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_PROPOSAL_SECONDS
    ceiling = max(1, min(configured, MAX_PROPOSAL_SECONDS))
    raw = args.get("duration_seconds", ceiling)
    if isinstance(raw, bool):
        raise ValueError("duration_seconds must be a positive integer")
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError("duration_seconds must be a positive integer")
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        raise ValueError("duration_seconds must be a positive integer") from None
    if requested < 1:
        raise ValueError("duration_seconds must be a positive integer")
    return min(requested, ceiling)


def _remaining(deadline: float, cap: int) -> int:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("proposal duration budget exhausted")
    return max(1, min(cap, int(seconds + 0.999)))




def _daily_limits() -> tuple[int, int]:
    """Return operator daily budgets, with finite hard ceilings."""
    try:
        seconds = int(os.getenv("AVRONE_EVOLVE_MAX_DAILY_SECONDS", str(DEFAULT_MAX_DAILY_SECONDS)))
    except (TypeError, ValueError):
        seconds = DEFAULT_MAX_DAILY_SECONDS
    try:
        proposals = int(os.getenv("AVRONE_EVOLVE_MAX_DAILY_PROPOSALS", str(DEFAULT_MAX_DAILY_PROPOSALS)))
    except (TypeError, ValueError):
        proposals = DEFAULT_MAX_DAILY_PROPOSALS
    return max(1, min(seconds, HARD_MAX_DAILY_SECONDS)), max(1, min(proposals, HARD_MAX_DAILY_PROPOSALS))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not accepted")


def _validate_budget_state(data: Any, day: str) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("day") != day:
        raise RuntimeError("daily evolution budget state is invalid; refusing new work")
    for key in ("started", "accepted", "rejected"):
        value = data.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("daily evolution budget state has invalid counters")
    for key in ("elapsed_s", "reserved_s"):
        value = data.get(key, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise RuntimeError("daily evolution budget state has invalid duration fields")
    if data["accepted"] + data["rejected"] > data["started"]:
        raise RuntimeError("daily evolution budget counters are inconsistent")
    return data


def _budget_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _budget_paths(ctx: "AgentContext", day: str) -> tuple[Path, Path]:
    directory = _data(ctx)
    return directory / f"budget-{day}.json", directory / "budget.lock"


def _mutate_budget(ctx: "AgentContext", day: str, change: Any) -> dict[str, Any]:
    """Atomically mutate the UTC-day budget across processes; corrupt state fails closed."""
    path, lock_path = _budget_paths(ctx, day)
    if path.is_symlink() or lock_path.is_symlink():
        raise RuntimeError("daily evolution budget files must not be symlinks")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if path.is_symlink():
                raise RuntimeError("daily evolution budget files must not be symlinks")
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
                except (OSError, ValueError, TypeError, UnicodeDecodeError):
                    raise RuntimeError("daily evolution budget state is corrupt; refusing new work") from None
                data = _validate_budget_state(data, day)
            else:
                data = {"day": day, "started": 0, "accepted": 0, "rejected": 0,
                        "elapsed_s": 0.0, "reserved_s": 0.0}
            change(data)
            fd, tmp_name = tempfile.mkstemp(prefix=".budget-", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    json.dump(data, tmp, sort_keys=True)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            return dict(data)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reserve_daily_budget(ctx: "AgentContext", timeout_s: int) -> dict[str, Any]:
    """Reserve the worst-case duration before execution so parallel proposals cannot oversubscribe."""
    day = _budget_day()
    max_seconds, max_proposals = _daily_limits()

    def reserve(data: dict[str, Any]) -> None:
        started = int(data.get("started", 0))
        elapsed = float(data.get("elapsed_s", 0))
        reserved = float(data.get("reserved_s", 0))
        if started >= max_proposals:
            raise ValueError(f"daily proposal limit reached ({max_proposals})")
        if elapsed + reserved + timeout_s > max_seconds:
            remaining = max(0, int(max_seconds - elapsed - reserved))
            raise ValueError(f"daily evolution time budget exhausted ({remaining}s remaining; proposal needs {timeout_s}s)")
        data["started"] = started + 1
        data["reserved_s"] = reserved + timeout_s

    _mutate_budget(ctx, day, reserve)
    return {"day": day, "timeout_s": timeout_s}


def _finish_daily_budget(ctx: "AgentContext", reservation: dict[str, Any], duration_s: float, passed: bool) -> dict[str, Any]:
    day = str(reservation["day"])
    reserved_s = int(reservation["timeout_s"])

    def finish(data: dict[str, Any]) -> None:
        data["reserved_s"] = max(0.0, float(data.get("reserved_s", 0)) - reserved_s)
        data["elapsed_s"] = max(0.0, float(data.get("elapsed_s", 0))) + max(0.0, duration_s)
        key = "accepted" if passed else "rejected"
        data[key] = int(data.get(key, 0)) + 1
        data["last_finished_utc"] = datetime.now(timezone.utc).isoformat()

    return _mutate_budget(ctx, day, finish)


def _daily_budget_snapshot(ctx: "AgentContext", day: str | None = None) -> dict[str, Any]:
    day = day or _budget_day()
    path, lock_path = _budget_paths(ctx, day)
    if path.is_symlink() or lock_path.is_symlink():
        raise RuntimeError("daily evolution budget files must not be symlinks")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if path.is_symlink():
                raise RuntimeError("daily evolution budget files must not be symlinks")
            if not path.exists():
                return {"day": day, "started": 0, "accepted": 0, "rejected": 0, "elapsed_s": 0.0, "reserved_s": 0.0}
            try:
                data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
            except (OSError, ValueError, TypeError, UnicodeDecodeError):
                raise RuntimeError("daily evolution budget state is corrupt") from None
            return dict(_validate_budget_state(data, day))
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _validate_edits(edits: Any) -> list[dict[str, str]]:
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list")
    if len(edits) > MAX_PROPOSAL_EDITS:
        raise ValueError(f"too many edits: maximum is {MAX_PROPOSAL_EDITS}")
    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edit {index + 1} must be an object")
        rel = edit.get("path")
        value = edit.get("content", "")
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError(f"edit {index + 1} path must be a non-empty string")
        candidate = Path(rel)
        if candidate.is_absolute() or "\\" in rel or "\x00" in rel:
            raise ValueError(f"edit path must be a relative POSIX path: {rel!r}")
        if not candidate.parts:
            raise ValueError("edit path must name a file")
        if any(part in {"..", ".git"} for part in candidate.parts):
            raise ValueError(f"edit path escapes repo or contains a forbidden component: {rel!r}")
        if not isinstance(value, str):
            raise ValueError(f"edit {index + 1} content must be a string")
        key = candidate.as_posix()
        if key in seen:
            raise ValueError(f"duplicate edit path: {key}")
        seen.add(key)
        size = len(value.encode("utf-8"))
        if size > MAX_EDIT_BYTES:
            raise ValueError(f"edit {key!r} exceeds {MAX_EDIT_BYTES} bytes")
        total += size
        if total > MAX_TOTAL_EDIT_BYTES:
            raise ValueError(f"proposal exceeds {MAX_TOTAL_EDIT_BYTES} total edit bytes")
        validated.append({"path": key, "content": value})
    return validated


def _validated_test_command(args: dict[str, Any]) -> str:
    """Allow pytest invocation only; shell operators are never accepted from an agent."""
    raw = args.get("test_cmd") or os.getenv("AVRONE_EVOLVE_TEST_CMD") or 'python -m pytest -q -m "not integration"'
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("test_cmd must invoke pytest")
    if SHELL_META.search(raw):
        raise ValueError("test_cmd contains shell control syntax; only a direct pytest invocation is allowed")
    try:
        parts = shlex.split(raw)
    except ValueError:
        raise ValueError("test_cmd quoting is invalid") from None
    if not parts:
        raise ValueError("test_cmd must invoke pytest")
    executable = Path(parts[0]).name.lower()
    is_pytest = executable in {"pytest", "pytest-3"}
    is_python_pytest = (
        executable in {"python", "python3", "python3.11", "python3.12", "python3.13"}
        and len(parts) >= 3 and parts[1:3] == ["-m", "pytest"]
    )
    if not (is_pytest or is_python_pytest):
        raise ValueError("test_cmd must be pytest or python -m pytest")
    return shlex.join(parts)


def _validated_pid(pid: str) -> str:
    if not isinstance(pid, str) or not PID_RE.fullmatch(pid) or pid in {".", ".."}:
        raise ValueError("invalid proposal id")
    return pid


def _meta_path(ctx: "AgentContext", pid: str) -> Path:
    return _data(ctx) / f"{_validated_pid(pid)}.json"


def _worktree_path(ctx: "AgentContext", pid: str) -> Path:
    return _data(ctx) / f"wt-{_validated_pid(pid)}"


async def _ensure_worktree(ctx: "AgentContext", pid: str, deadline: float) -> tuple[Path, str]:
    """Create an isolated proposal clone, respecting the proposal deadline."""
    repo = _repo_root(ctx).resolve()
    wt = _worktree_path(ctx, pid)
    branch = f"evolve/{pid}"
    if wt.is_symlink():
        raise ValueError("proposal worktree path must not be a symlink")
    if wt.exists():
        raise FileExistsError("proposal worktree path already exists; reset it before retrying")
    r = await _sh(
        ctx, f"git clone -q --no-hardlinks . {shlex.quote(str(wt))} 2>&1",
        repo, timeout=_remaining(deadline, 600),
    )
    if r.exit_code != 0:
        _remaining(deadline, 120)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        shutil.copytree(repo, wt, ignore=shutil.ignore_patterns(
            ".git", "data", "*.egg-info", "__pycache__", ".pytest_cache", ".venv", "venv"
        ))
        initialized = await _sh(
            ctx,
            "git init -q && git add -A && git -c user.email=avrone@localhost "
            "-c user.name=avrone commit -qm base",
            wt, timeout=_remaining(deadline, 120),
        )
        if initialized.exit_code != 0:
            raise RuntimeError("could not initialize isolated proposal worktree")
    checkout = await _sh(
        ctx, f"git checkout -q -b {shlex.quote(branch)}", wt,
        timeout=_remaining(deadline, 120),
    )
    if checkout.exit_code != 0:
        raise RuntimeError(f"could not create proposal branch {branch}")
    return wt, branch


def _in_repo(wt: Path, rel: str) -> Path:
    root = wt.resolve()
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError(f"path must be relative to repo: {rel}")
    p = (root / candidate).resolve()
    try:
        relative = p.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes repo: {rel}") from None
    if not relative.parts:
        raise ValueError("edit path must target a file inside the repo")
    if ".git" in relative.parts:
        raise ValueError("edits to Git metadata are forbidden")
    return p


async def t_evolve_propose(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if os.getenv("AVRONE_SELF_EVOLVE", "allow").lower() == "deny":
        return "ERROR: self-evolution is disabled on this node (AVRONE_SELF_EVOLVE=deny)"
    goal = str(args.get("goal", "")).strip()
    if not goal:
        return "ERROR: goal is required"
    if len(goal) > 500:
        return "ERROR: goal must be at most 500 characters"
    try:
        edits = _validate_edits(args.get("edits"))
        timeout_s = _proposal_timeout(args)
        test_cmd = _validated_test_command(args)
    except ValueError as exc:
        return f"ERROR: {exc}"

    try:
        budget_reservation = _reserve_daily_budget(ctx, timeout_s)
    except (OSError, ValueError, RuntimeError) as exc:
        return f"ERROR: self-evolution budget denied: {exc}"

    import uuid
    pid = SAFE_ID.sub("-", f"{int(time.time())}-{ctx.name}-{uuid.uuid4().hex[:6]}")[:56]
    started_wall = time.time()
    started_mono = time.monotonic()
    deadline = started_mono + timeout_s
    wt: Path | None = None
    branch = f"evolve/{pid}"
    changed: list[str] = []
    passed = False
    flaky = False
    tail = ""
    test_result = None
    failure = ""

    try:
        wt, branch = await _ensure_worktree(ctx, pid, deadline)
        for edit in edits:
            _remaining(deadline, 1)
            p = _in_repo(wt, edit["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(edit["content"], encoding="utf-8")
            changed.append(edit["path"])

        # One deadline covers clone/setup, edits, test execution, retry, and commit.
        test_env = "PYTHONPATH=" + shlex.quote(str(wt)) + "${PYTHONPATH:+:$PYTHONPATH}"
        res = await _sh(
            ctx, f"{test_env} {test_cmd} 2>&1", wt,
            timeout=_remaining(deadline, timeout_s),
        )
        test_result = res
        passed = res.exit_code == 0

        if not passed and "pytest" in test_cmd:
            try:
                retry_timeout = _remaining(deadline, min(600, timeout_s))
            except TimeoutError:
                retry_timeout = 0
            if retry_timeout:
                again = await _sh(
                    ctx, f"{test_env} {test_cmd} 2>&1", wt,
                    timeout=retry_timeout,
                )
                test_result = again
                if again.exit_code == 0:
                    passed, flaky = True, True

        output = (getattr(test_result, "stdout", "") or "") + (getattr(test_result, "stderr", "") or "")
        tail = "\n".join(output.splitlines()[-25:])
        if passed:
            commit = await _sh(
                ctx,
                "git add -A && git -c user.email=avrone@localhost -c user.name=avrone "
                f"commit -qm {shlex.quote(goal)}",
                wt, timeout=_remaining(deadline, 120),
            )
            if commit.exit_code != 0:
                passed = False
                failure = "tests passed, but proposal commit failed"
                tail = (tail + "\n" + failure).strip()
    except TimeoutError as exc:
        passed = False
        failure = str(exc) or "proposal duration budget exhausted"
        tail = (tail + "\n" + failure).strip()
    except Exception as exc:  # noqa: BLE001
        passed = False
        failure = f"{type(exc).__name__}: {exc}"
        tail = (tail + "\n" + failure).strip()

    duration_s = round(max(0.0, time.monotonic() - started_mono), 3)
    try:
        _finish_daily_budget(ctx, budget_reservation, duration_s, passed)
    except Exception as exc:  # noqa: BLE001
        passed = False
        failure = f"daily budget accounting failed: {type(exc).__name__}: {exc}"
        tail = (tail + "\n" + failure).strip()
    meta = {
        "pid": pid, "goal": goal, "branch": branch, "files": changed,
        "passed": passed, "by": ctx.name, "ts": started_wall,
        "duration_s": duration_s, "timeout_s": timeout_s,
        "budget_day_utc": budget_reservation["day"],
        "test_cmd": test_cmd, "worktree": str(wt) if wt else "",
        "flaky_retry": flaky, "failure": failure, "test_tail": tail[-3000:],
    }
    _meta_path(ctx, pid).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _emit(ctx, "evolve.propose", pid=pid, passed=passed, files=changed,
          duration_s=duration_s, timeout_s=timeout_s, flaky_retry=flaky, failure=failure)
    if passed:
        verdict = "ACCEPTED (tests pass" + (" after a retry" if flaky else "") + f", committed to {branch})"
    else:
        verdict = "REJECTED (validation or test gate did not pass)"
    return (
        f"proposal {pid}: {verdict}\ngoal: {goal}\nfiles: {', '.join(changed)}\n"
        f"duration: {duration_s:.3f}s / {timeout_s}s\n"
        f"failure: {failure or 'none'}\n--- test tail ---\n{tail}\n"
        f"{'Use evolve_diff ' + pid + ' to review the change.' if passed else 'Inspect the recorded failure and retry with a corrected proposal.'}"
    )


async def t_evolve_diff(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        pid = _validated_pid(str(args.get("pid", "")))
        mp = _meta_path(ctx, pid)
        wt = _worktree_path(ctx, pid)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not mp.exists():
        return f"ERROR: no proposal {pid}. evolve_list to see them."
    if not wt.is_dir() or wt.is_symlink():
        return f"ERROR: proposal worktree is missing or invalid: {pid}"
    r = await _sh(ctx, "git --no-pager diff HEAD~1..HEAD 2>/dev/null || git --no-pager diff", wt)
    return r.text(8000) or "(no diff)"


def _load_proposals(ctx: "AgentContext") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(_data(ctx).glob("*.json")):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(item, dict) and isinstance(item.get("pid"), str):
            rows.append(item)
    return rows


async def t_evolve_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    rows = []
    for m in _load_proposals(ctx):
        pid = m.get("pid", "?")
        passed = bool(m.get("passed"))
        goal = str(m.get("goal", ""))[:70]
        files = m.get("files") if isinstance(m.get("files"), list) else []
        duration = m.get("duration_s")
        elapsed = f"{float(duration):.2f}s" if isinstance(duration, (int, float)) else "duration unknown"
        rows.append(f"{pid} [{'PASS' if passed else 'FAIL'}] {elapsed} {goal} "
                    f"({', '.join(str(p) for p in files)}) by {m.get('by', '?')}")
    return "\n".join(rows) or "no proposals yet"


async def t_evolve_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    proposals = _load_proposals(ctx)
    accepted = [p for p in proposals if p.get("passed") is True]
    rejected = [p for p in proposals if p.get("passed") is not True]
    durations = [float(p["duration_s"]) for p in proposals
                 if isinstance(p.get("duration_s"), (int, float)) and p["duration_s"] >= 0]
    total = sum(durations)
    average = total / len(durations) if durations else 0.0
    cap = _proposal_timeout({})
    daily_seconds, daily_proposals = _daily_limits()
    try:
        today = _daily_budget_snapshot(ctx)
        today_line = (f"UTC day: {today['day']}; started {today.get('started', 0)}/{daily_proposals}; "
                      f"accepted {today.get('accepted', 0)}; rejected {today.get('rejected', 0)}; "
                      f"elapsed {float(today.get('elapsed_s', 0)):.2f}s; "
                      f"reserved {float(today.get('reserved_s', 0)):.2f}s / {daily_seconds}s")
    except RuntimeError as exc:
        today_line = f"UTC-day budget status unavailable: {exc}"
    return (
        "AVRONE self-evolution budget\n"
        f"proposals: {len(proposals)} total; {len(accepted)} accepted; {len(rejected)} rejected\n"
        f"recorded runtime: {total:.2f}s total; {average:.2f}s average per proposal\n"
        f"per-proposal duration ceiling: {cap}s (operator ceiling AVRONE_EVOLVE_MAX_SECONDS; hard max {MAX_PROPOSAL_SECONDS}s)\n"
        f"daily ceiling: {daily_seconds}s and {daily_proposals} started proposals (hard maxima {HARD_MAX_DAILY_SECONDS}s and {HARD_MAX_DAILY_PROPOSALS} proposals)\n"
        f"{today_line}\n"
        f"input limits: {MAX_PROPOSAL_EDITS} edits; {MAX_EDIT_BYTES} bytes/file; {MAX_TOTAL_EDIT_BYTES} bytes/proposal\n"
        "loop status: every proposal needs validation and a test gate; no autonomous infinite loop is started"
    )


async def t_evolve_reset(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        pid = _validated_pid(str(args.get("pid", "")))
        mp = _meta_path(ctx, pid)
        wt = _worktree_path(ctx, pid)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not mp.exists():
        return f"ERROR: no proposal {pid}"
    try:
        if wt.is_symlink():
            wt.unlink()
        else:
            shutil.rmtree(wt, ignore_errors=True)
        mp.unlink()
    except OSError as exc:
        return f"ERROR: could not reset proposal {pid}: {exc}"
    _emit(ctx, "evolve.reset", pid=pid)
    return f"discarded proposal {pid}"


EVOLVE_TOOLS = [
    ("evolve_propose", "propose a change to AVRONE's OWN code: it is applied in a throwaway copy of the repo and the "
     "test suite is run; it is kept only if every test passes. Nothing touches the live tree or main.",
     '{"goal": str, "edits": [{"path": str, "content": str}], "test_cmd"?: str}', t_evolve_propose),
    ("evolve_diff", "show the unified diff of an accepted proposal.", '{"pid": str}', t_evolve_diff),
    ("evolve_list", "list self-evolution proposals and their outcomes and durations.", "{}", t_evolve_list),
    ("evolve_status", "summarize accepted/rejected proposals, recorded duration, hard limits, and loop status.", "{}", t_evolve_status),
    ("evolve_reset", "discard a proposal and its validated isolated worktree.", '{"pid": str}', t_evolve_reset),
]


def register_evolve_tools(registry) -> None:
    for name, desc, args_doc, fn in EVOLVE_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))


def list_accepted(settings) -> list[dict[str, Any]]:
    """Accepted proposals, for /api/evolve and the CLI."""
    root = Path(settings.data_dir).resolve()
    d = root / "evolve"
    if d.is_symlink():
        return []
    out = []
    if d.exists():
        for f in sorted(d.glob("*.json")):
            try:
                m = json.loads(f.read_text())
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(m, dict) or m.get("passed") is not True:
                continue
            out.append({k: m[k] for k in ("pid", "goal", "branch", "files", "by", "ts", "duration_s", "timeout_s") if k in m})
    return out
