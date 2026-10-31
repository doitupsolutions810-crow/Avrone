"""gap_scan + gap_mission: the systems-gap-bridge tools (engine in avrone.gapsystem)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .gapsystem import (GAP_MISSION_MAX_TOPIC, GAP_SCOPES, GAP_SCAN_MAX_CHARS,
                        GAP_SCAN_MAX_FINDINGS, repo_root, scan)
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext


def _cap(text: str) -> str:
    return text if len(text) <= GAP_SCAN_MAX_CHARS else text[:GAP_SCAN_MAX_CHARS] + "\n...[truncated]"


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    codex = getattr(getattr(ctx, "core", None), "codex", None)
    if codex is not None:
        try:
            codex.emit(ctx.run_id, kind, ctx.name, **data)
        except Exception:  # noqa: BLE001
            pass


def _no_repo() -> str:
    return ("ERROR: no AVRONE checkout found (need pyproject.toml + avrone/__init__.py under "
            "AVRONE_REPO_DIR, the node's repo_dir or the workspace)")


async def t_gap_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    scope = str(args.get("scope") or "all").strip().lower()
    if scope not in GAP_SCOPES:
        return "ERROR: scope must be one of: " + ", ".join(GAP_SCOPES)
    root = repo_root(ctx)
    if root is None:
        return _no_repo()
    rep = scan(root)
    findings = rep["findings"] if scope == "all" else [f for f in rep["findings"] if f["scope"] == scope]
    total = len(findings)
    if str(args.get("format") or "text").lower() == "json":
        out = {"scope": scope, "root": rep["root"], "scanned_files": rep["scanned_files"],
               "total": total, "findings": findings[:GAP_SCAN_MAX_FINDINGS],
               "hidden": max(0, total - GAP_SCAN_MAX_FINDINGS)}
        return _cap(json.dumps(out, indent=1, sort_keys=True))
    lines = [f"gap_scan [{scope}] under {rep['root']}: {total} finding(s), "
             f"{rep['scanned_files']} file(s) scanned"]
    for f in findings[:GAP_SCAN_MAX_FINDINGS]:
        lines.append(f"- {f['scope']}/{f['kind']}: {f['detail']}")
    hidden = max(0, total - GAP_SCAN_MAX_FINDINGS)
    if hidden:
        lines.append(f"...[{hidden} more finding(s) hidden]")
    if not total:
        lines.append("no gaps found in this scope; the system is coherent here")
    return _cap("\n".join(lines))


async def t_gap_mission(ctx: "AgentContext", args: dict[str, Any]) -> str:
    topic = str(args.get("topic") or "").strip()[:GAP_MISSION_MAX_TOPIC]
    root = repo_root(ctx)
    if root is None:
        return _no_repo()
    rep = scan(root)
    _emit(ctx, "gap.mission", tool="gap_mission", topic=topic, total=rep["total"],
          by_scope=json.dumps(rep["by_scope"], sort_keys=True))
    counts = "".join(f", {k}={v}" for k, v in sorted(rep["by_scope"].items()))
    lines = [
        f"gap mission: {topic or 'long-horizon systems analysis'}",
        f"coherence scan of {rep['root']} ({rep['scanned_files']} file(s)): "
        f"{rep['total']} finding(s)" + (counts if counts else ", none"),
        "mission brief for the squad:",
        "1. gap_scan each scope with findings above; confirm each one by hand (inspect_tool, "
        "registry_audit, read_file) before acting.",
        "2. bridge_post this brief to the run bridge as kind question; add one task_board card per "
        "confirmed finding so the mission has an owner.",
        "3. cross-exam the findings on the bridge (bridge_read, bridge_reply); research_note what "
        "survives as kind finding so the recursive learning loop picks it up.",
        "4. land fixes only via evolve_propose (throwaway copy, full test suite must pass); re-run "
        "gap_scan after the merge and record the evidence with great_scale.",
        "lesson_recall first: an earlier run may already have learned a finding; lesson_forget "
        "retracts what proved wrong.",
    ]
    return _cap("\n".join(lines))


GAPSYSTEM_TOOLS: list[Tool] = [
    Tool("gap_scan", "read-only: coherence scan of AVRONE's OWN checkout (skills frontmatter/steps, "
         "AVRONE_* env grounding, version drift, register_*_tools wiring, load-cap mapping); bounded, "
         "offline, never prints file contents.",
         '{"scope"?: "all"|"skills"|"env"|"versions"|"wiring"|"caps", "format"?: "text"|"json"}',
         t_gap_scan, source="builtin",
         schema={"optional": {"scope": "str", "format": "str"}}),
    Tool("gap_mission", "acting: run the coherence scan, distill it into a long-horizon mission brief "
         "(goal, per-scope counts, first steps) and log it to the Eternal CodeX; returns the brief to "
         "emit to the squad via bridge_post / task_board.",
         '{"topic"?: str}', t_gap_mission, source="builtin",
         schema={"optional": {"topic": "str"}}),
]


def register_gapsystem_tools(registry) -> None:
    for t in GAPSYSTEM_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                              schema=dict(t.schema) if t.schema is not None else None))
