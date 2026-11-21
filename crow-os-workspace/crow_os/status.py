"""Status taxonomy and verification gating.

A change is `verified` only when every acceptance gate is actually passing.
Nothing in this module accepts a caller-supplied verified flag.
"""

from __future__ import annotations

from typing import Any, Iterable

CHIP_ORDER = (
    "draft",
    "open",
    "merged",
    "unmerged",
    "failed",
    "blocked",
    "verified",
    "unverified",
)

FAILURE_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}
PASSING_CONCLUSIONS = {"success", "skipped", "neutral"}

HUMAN_GATE_ID = "operator_accepts_evidence"
HUMAN_GATE_NAME = "Operator accepts the recorded evidence"


class VerificationRejected(Exception):
    """Raised when a caller tries to record verification the gates do not allow."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons or ["verification gates have not all passed"]
        super().__init__("; ".join(self.reasons))


def _ordered_chips(flags: set[str]) -> list[str]:
    if "verified" in flags:
        flags.discard("unverified")
    else:
        flags.add("unverified")
    if "merged" in flags:
        flags.discard("unmerged")
    return [chip for chip in CHIP_ORDER if chip in flags]


def _index_files(rnd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in (rnd.get("files") or []) if item.get("path")}


def suite_round_matches(rnd: dict[str, Any], change: dict[str, Any], required: list[str], *, require_hash: bool) -> bool:
    """True when one Python acceptance round covers the current commit."""
    if rnd.get("runner") != "python":
        return False
    if not rnd.get("acceptance"):
        return False
    if rnd.get("status") != "passed":
        return False
    if not required:
        return False
    if (rnd.get("commit_sha") or "") != (change.get("sha") or ""):
        return False
    if require_hash and not rnd.get("log_sha256"):
        return False
    by_path = _index_files(rnd)
    for path in required:
        item = by_path.get(path)
        if not item or item.get("status") != "passed":
            return False
        if require_hash and not item.get("log_sha256"):
            return False
    for item in by_path.values():
        if item.get("status") in {"failed", "error"}:
            return False
    return True


def _syntax_gate(required: list[str], file_syntax: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not required:
        return _gate(
            "syntax_clean",
            "Acceptance files compile",
            "automatic",
            False,
            "No acceptance files are declared, so this gate cannot pass.",
        )
    missing = [path for path in required if path not in file_syntax]
    broken = []
    for path in required:
        info = file_syntax.get(path)
        if info and not info.get("syntax_ok", False):
            broken.append(f"{path}: {info.get('syntax_error') or 'does not compile'}")
    if missing:
        detail = "Not present at this commit: " + ", ".join(missing[:8])
        return _gate("syntax_clean", "Acceptance files compile", "automatic", False, detail)
    if broken:
        return _gate(
            "syntax_clean",
            "Acceptance files compile",
            "automatic",
            False,
            "Does not compile: " + "; ".join(broken[:6]),
        )
    return _gate(
        "syntax_clean",
        "Acceptance files compile",
        "automatic",
        True,
        f"All {len(required)} acceptance files compile.",
    )


def _gate(gate_id: str, name: str, kind: str, passed: bool, detail: str) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "name": name,
        "kind": kind,
        "passed": bool(passed),
        "detail": detail,
    }


def _ci_gate(change: dict[str, Any], checks: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not change.get("pr_number") or checks is None:
        return None
    if not checks:
        return _gate(
            "ci_green",
            "GitHub checks are green",
            "automatic",
            False,
            "GitHub returned no check runs for this commit.",
        )
    bad = []
    for check in checks:
        conclusion = (check.get("conclusion") or "").lower()
        status = (check.get("status") or "").lower()
        if status != "completed" or conclusion not in PASSING_CONCLUSIONS:
            bad.append(f"{check.get('name') or 'check'} ({conclusion or status or 'unknown'})")
    if bad:
        return _gate(
            "ci_green",
            "GitHub checks are green",
            "automatic",
            False,
            "Failing or incomplete checks: " + ", ".join(bad[:8]),
        )
    return _gate(
        "ci_green",
        "GitHub checks are green",
        "automatic",
        True,
        f"{len(checks)} check runs completed without a failure.",
    )


def pick_next_gate(gates: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_id = {gate["gate_id"]: gate for gate in gates}
    for gate_id in ("syntax_clean", "fixes_reverified", "full_suite", "evidence_hashed", "ci_green"):
        gate = by_id.get(gate_id)
        if gate and not gate["passed"]:
            return gate
    for gate in gates:
        if gate["kind"] == "human" and not gate["passed"]:
            return gate
    return None


def evaluate_change(
    change: dict[str, Any],
    *,
    required_files: Iterable[str],
    file_syntax: dict[str, dict[str, Any]] | None = None,
    rounds: Iterable[dict[str, Any]] | None = None,
    fixes: Iterable[dict[str, Any]] | None = None,
    human_gates: Iterable[dict[str, Any]] | None = None,
    pull: dict[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute chips and gates. `verified` is added only when every gate passes."""
    required = list(required_files)
    file_syntax = file_syntax or {}
    round_list = list(rounds or [])
    fix_list = list(fixes or [])
    gates: list[dict[str, Any]] = [
        _syntax_gate(required, file_syntax),
    ]
    open_fixes = [item for item in fix_list if item.get("status") in {"open", "linked"}]
    if open_fixes:
        preview = ", ".join(
            f"{item.get('file_path') or 'file'} ({item.get('status')})" for item in open_fixes[:6]
        )
        gates.append(
            _gate(
                "fixes_reverified",
                "Recorded failures are cleared by a later run",
                "automatic",
                False,
                f"{len(open_fixes)} fix item(s) still open or only linked: {preview}. "
                "Linking a commit does not clear a failure; a re-run has to pass.",
            )
        )
    else:
        gates.append(
            _gate(
                "fixes_reverified",
                "Recorded failures are cleared by a later run",
                "automatic",
                True,
                "No open or linked fix items remain on this change.",
            )
        )

    suite_hit = next(
        (rnd for rnd in round_list if suite_round_matches(rnd, change, required, require_hash=False)),
        None,
    )
    if suite_hit:
        gates.append(
            _gate(
                "full_suite",
                "Full acceptance suite passed in one Python round",
                "automatic",
                True,
                f"Round {suite_hit.get('id')} passed on {change.get('sha', '')[:12]}.",
            )
        )
    else:
        advisory = [rnd for rnd in round_list if rnd.get("runner") == "ollama"]
        detail = (
            f"{len(required)} acceptance file(s) need one passing local Python round "
            f"on commit {str(change.get('sha') or '')[:12] or '(none)'}."
        )
        if advisory:
            detail += " Ollama output is advisory and cannot satisfy this gate."
        gates.append(
            _gate(
                "full_suite",
                "Full acceptance suite passed in one Python round",
                "automatic",
                False,
                detail,
            )
        )

    hashed = next(
        (rnd for rnd in round_list if suite_round_matches(rnd, change, required, require_hash=True)),
        None,
    )
    if hashed:
        gates.append(
            _gate(
                "evidence_hashed",
                "Round evidence has SHA-256 hashes",
                "automatic",
                True,
                f"Log {hashed.get('log_sha256')} covers the passing round.",
            )
        )
    else:
        gates.append(
            _gate(
                "evidence_hashed",
                "Round evidence has SHA-256 hashes",
                "automatic",
                False,
                "No passing Python acceptance round on this commit has a log hash and per-file hashes.",
            )
        )

    ci = _ci_gate(change, checks)
    if ci:
        gates.append(ci)

    for human in human_gates or []:
        gates.append(
            _gate(
                human.get("gate_id") or HUMAN_GATE_ID,
                human.get("name") or HUMAN_GATE_NAME,
                "human",
                bool(human.get("passed")),
                human.get("detail")
                or (
                    "Operator attested the evidence."
                    if human.get("passed")
                    else "Waiting for an operator to accept the recorded evidence."
                ),
            )
        )

    can_verify = bool(gates) and all(gate["passed"] for gate in gates)
    blocking = [f"{gate['name']}: {gate['detail']}" for gate in gates if not gate["passed"]]

    syntax_broken = not gates[0]["passed"] and bool(required) and any(
        path in file_syntax and not file_syntax[path].get("syntax_ok", False) for path in required
    )
    round_failed = any(
        rnd.get("acceptance") and rnd.get("commit_sha") == change.get("sha") and rnd.get("status") == "failed"
        for rnd in round_list
    )
    checks_bad = False
    if checks:
        for check in checks:
            conclusion = (check.get("conclusion") or "").lower()
            status = (check.get("status") or "").lower()
            if status != "completed" or conclusion in FAILURE_CONCLUSIONS:
                checks_bad = True
                break

    flags: set[str] = set()
    if pull:
        if pull.get("draft"):
            flags.add("draft")
        if pull.get("merged"):
            flags.add("merged")
        else:
            flags.add("unmerged")
            if (pull.get("state") or "open") == "open":
                flags.add("open")
    elif change.get("contained_in_main") is True:
        flags.add("merged")
    else:
        flags.add("unmerged")

    if open_fixes or (pull and pull.get("state") == "open"):
        flags.add("open")
    if syntax_broken or round_failed or checks_bad or open_fixes:
        flags.add("failed")
    if syntax_broken or open_fixes:
        flags.add("blocked")
    if can_verify:
        flags.add("verified")
    else:
        flags.add("unverified")

    next_gate = pick_next_gate(gates)
    if next_gate is None:
        if "unmerged" in flags:
            next_gate = _gate(
                "github_merge",
                "GitHub merge",
                "informational",
                False,
                "Verification gates passed. This change stays unmerged until GitHub reports it merged.",
            )
        else:
            next_gate = _gate(
                "none",
                "No verification gate outstanding",
                "informational",
                True,
                "The acceptance gates passed and GitHub reports the change merged.",
            )

    return {
        "change_id": change.get("id"),
        "title": change.get("title"),
        "sha": change.get("sha"),
        "chips": _ordered_chips(flags),
        "can_verify": can_verify,
        "gates": gates,
        "next_gate": next_gate,
        "blocking": blocking,
    }


def verification_decision(evaluation: dict[str, Any]) -> None:
    """Reject a verification attempt unless the computed gates all pass."""
    if not evaluation.get("can_verify"):
        raise VerificationRejected(list(evaluation.get("blocking") or []))


def rank_next(evaluation: dict[str, Any]) -> int:
    gate_id = (evaluation.get("next_gate") or {}).get("gate_id")
    weights = {
        "syntax_clean": 100,
        "fixes_reverified": 80,
        "full_suite": 60,
        "evidence_hashed": 40,
        "ci_green": 30,
        HUMAN_GATE_ID: 10,
        "github_merge": 5,
        "none": 0,
    }
    score = weights.get(gate_id, 10)
    chips = set(evaluation.get("chips") or [])
    if "blocked" in chips:
        score += 3
    if "failed" in chips:
        score += 2
    return score


def choose_workspace_gate(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    if not evaluations:
        return {
            "change_id": None,
            "change_title": None,
            "chips": ["unverified"],
            "gate": _gate(
                "full_suite",
                "Full acceptance suite passed in one Python round",
                "automatic",
                False,
                "No changes have been ingested yet.",
            ),
            "others_waiting": 0,
        }
    ranked = sorted(evaluations, key=lambda item: (-rank_next(item), item.get("title") or ""))
    chosen = ranked[0]
    waiting = sum(1 for item in evaluations if not item.get("can_verify"))
    return {
        "change_id": chosen.get("change_id"),
        "change_title": chosen.get("title"),
        "chips": chosen.get("chips") or [],
        "gate": chosen.get("next_gate"),
        "others_waiting": max(0, waiting - (0 if chosen.get("can_verify") else 1)),
    }


def propagation_graph(
    *,
    changes: Iterable[dict[str, Any]],
    syntax_failures: Iterable[dict[str, Any]],
    fixes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Failure -> gate -> change/PR, plus component nodes named in the file."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    def add_node(node_id: str, kind: str, label: str, detail: str = "") -> None:
        nodes.setdefault(node_id, {"id": node_id, "type": kind, "label": label, "detail": detail})

    def add_edge(src: str, dst: str, reason: str) -> None:
        edges.append({"from": src, "to": dst, "reason": reason})

    change_ids = {}
    for change in changes:
        cid = f"change:{change['id']}"
        change_ids[change["id"]] = cid
        add_node(cid, "change", change.get("title") or change["id"], change.get("ref") or "")
        if change.get("pr_number"):
            pid = f"pr:{change['pr_number']}"
            add_node(pid, "pr", f"PR #{change['pr_number']}", change.get("title") or "")
            add_edge(cid, pid, "tracks")

    for failure in syntax_failures:
        change_id = failure["change_id"]
        fid = f"fail:{change_id}:{failure['path']}"
        add_node(fid, "failure", failure["path"], failure.get("syntax_error") or "does not compile")
        gid = f"gate:{change_id}:syntax_clean"
        add_node(gid, "gate", "syntax_clean", "Acceptance files compile")
        add_edge(fid, gid, "blocks")
        target = change_ids.get(change_id) or f"change:{change_id}"
        add_node(target, "change", change_id, "")
        add_edge(gid, target, "blocks verification")
        components = failure.get("components") or []
        if not components:
            components = [failure["path"].rsplit("/", 1)[-1]]
        for component in components:
            comp_id = f"component:{component}"
            add_node(comp_id, "component", component, "named by the failing file")
            add_edge(fid, comp_id, "blocks component")

    for fix in fixes:
        if fix.get("status") not in {"open", "linked"}:
            continue
        change_id = fix.get("change_id") or "unassigned"
        fid = f"fix:{fix['id']}"
        label = fix.get("case_name") or fix.get("file_path") or fix["id"]
        add_node(fid, "failure", label, (fix.get("trace") or "")[:500])
        gid = f"gate:{change_id}:fixes_reverified"
        add_node(gid, "gate", "fixes_reverified", "A later run has to pass")
        add_edge(fid, gid, "blocks")
        target = change_ids.get(change_id) or f"change:{change_id}"
        add_node(target, "change", change_id, "")
        add_edge(gid, target, "blocks verification")
        if fix.get("fix_pr"):
            pid = f"pr:{fix['fix_pr']}"
            add_node(pid, "pr", f"PR #{fix['fix_pr']}", "linked fix, still unverified")
            add_edge(fid, pid, "blocks until re-verified")
        component = (fix.get("file_path") or "case").rsplit("/", 1)[-1]
        comp_id = f"component:{component}"
        add_node(comp_id, "component", component, "")
        add_edge(fid, comp_id, "blocks component")

    return {"nodes": list(nodes.values()), "edges": edges}
