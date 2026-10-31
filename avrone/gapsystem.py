"""gapsystem: the coherence engine behind gap_scan and gap_mission (skills/systems-gap-bridge).

    gap_scan {"scope"?: "all"|"skills"|"env"|"versions"|"wiring"|"caps", "format"?: "text"|"json"}
        Read-only static coherence scan of AVRONE's OWN checkout (located via AVRONE_REPO_DIR,
        else the node's repo_dir, else the workspace; the first candidate holding pyproject.toml
        + avrone/__init__.py wins):

          versions  pyproject.toml vs avrone/__init__.py vs the newest README "## vX.Y" heading
          skills    every skills/<name>/SKILL.md: frontmatter (name + description, name == dir),
                    contiguous numbered steps, evolve.check_skill when importable; dirs with no
                    SKILL.md; duplicate skill names
          env       every AVRONE_* a skill names must appear somewhere in avrone/*.py source
          wiring    every `def register_*_tools` in avrone/*.py must be called by
                    avrone/tools_extra.py's register_extra_tools
          caps      every load cap from tools_limits._limits() must be mapped in
                    skills/load-limits/SKILL.md

        Bounded: at most GAP_SCAN_MAX_FILES files, GAP_SCAN_MAX_FILE_BYTES per file,
        GAP_SCAN_MAX_FINDINGS findings listed, GAP_SCAN_MAX_CHARS of output. Never prints file
        contents - only names, paths, versions and env keys. No network, no writes.

    gap_mission {"topic"?: str}
        Acting: run the same scan, distill it into a long-horizon mission brief (goal, per-scope
        counts, first steps) and log it to the Eternal CodeX (kind "gap.mission"). Returns the
        brief; the caller emits it to the squad with bridge_post / task_board.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .trinity import AgentContext

GAP_SCOPES = ("all", "skills", "env", "versions", "wiring", "caps")
GAP_SCAN_MAX_FILES = 400
GAP_SCAN_MAX_FILE_BYTES = 262_144
GAP_SCAN_MAX_FINDINGS = 50
GAP_SCAN_MAX_CHARS = 12000
GAP_MISSION_MAX_TOPIC = 200

_ENV_RE = re.compile(r"AVRONE_[A-Z][A-Z0-9_]*")
_INIT_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
_PYPROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*["\']([^"\']+)["\']')
_README_RELEASE_RE = re.compile(r"(?m)^## v(\d+)\.(\d+)")
_REGISTER_DEF_RE = re.compile(r"(?m)^def (register_[a-z0-9_]+_tools)\s*\(")
_STEPS_RE = re.compile(r"(?m)^(\d+)\. ")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _read(p: Path) -> str:
    """One bounded text read; unreadable files read as empty, never raise."""
    try:
        if p.stat().st_size > GAP_SCAN_MAX_FILE_BYTES:
            return p.read_bytes()[:GAP_SCAN_MAX_FILE_BYTES].decode("utf-8", errors="replace") + "\n...[truncated]"
        return p.read_text(errors="replace")
    except OSError:
        return ""


def repo_root(ctx: "AgentContext") -> Path | None:
    """The AVRONE checkout to scan: AVRONE_REPO_DIR, else the node's repo_dir, else the workspace;
    the first candidate holding pyproject.toml and avrone/__init__.py wins."""
    cands: list[Path] = []
    env = os.environ.get("AVRONE_REPO_DIR", "").strip()
    if env:
        cands.append(Path(env))
    settings = getattr(getattr(ctx, "core", None), "settings", None)
    repo = getattr(settings, "repo_dir", None)
    if repo:
        cands.append(Path(str(repo)))
    runner = getattr(getattr(ctx, "core", None), "runner", None)
    ws = getattr(runner, "workspace", None)
    if ws:
        cands.append(Path(str(ws)))
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if (r / "pyproject.toml").is_file() and (r / "avrone" / "__init__.py").is_file():
            return r
    return None


def _scan_versions(root: Path, src: dict[str, str], add) -> None:
    init_v = None
    m = _INIT_VERSION_RE.search(src.get("__init__.py", ""))
    if m:
        init_v = m.group(1)
    else:
        add("versions", "missing", "avrone/__init__.py has no __version__")
    pp_v = None
    m = _PYPROJECT_VERSION_RE.search(_read(root / "pyproject.toml"))
    if m:
        pp_v = m.group(1)
    else:
        add("versions", "missing", "pyproject.toml has no version line")
    if init_v and pp_v and init_v != pp_v:
        add("versions", "drift", f"pyproject.toml {pp_v} != avrone/__init__.py {init_v}")
    if init_v:
        try:
            minor = tuple(int(x) for x in init_v.split(".")[:2])
        except ValueError:
            minor = None
        if minor is None:
            add("versions", "parse", f"cannot parse version {init_v[:32]!r} from avrone/__init__.py")
            return
        heads = _README_RELEASE_RE.findall(_read(root / "README.md"))
        if not heads:
            add("versions", "readme", "README.md has no '## vX.Y' release section")
        elif tuple(int(x) for x in heads[0]) != minor:
            add("versions", "readme",
                f"README's newest section is v{heads[0][0]}.{heads[0][1]}, "
                f"code is {'.'.join(str(x) for x in minor)}")


def _scan_skills(root: Path, add) -> list[tuple[str, str]]:
    """(skill_name, body) for every readable skills/*/SKILL.md, plus structural findings."""
    try:
        from .evolve import check_skill
    except Exception:  # noqa: BLE001
        check_skill = None
    skills_dir = root / "skills"
    bodies: list[tuple[str, str]] = []
    if not skills_dir.is_dir():
        add("skills", "missing", "no skills/ directory under the checkout")
        return bodies
    seen: dict[str, str] = {}
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.is_file():
            add("skills", "missing", f"skills/{d.name}/ has no SKILL.md")
            continue
        body = _read(f)
        bodies.append((d.name, body))
        meta: dict[str, str] = {}
        fm = _FRONTMATTER_RE.match(body)
        if fm:
            for line in fm.group(1).splitlines():
                k, _, v = line.partition(":")
                if k.strip() and v.strip():
                    meta[k.strip()] = v.strip()
        if not meta.get("name"):
            add("skills", "frontmatter", f"skills/{d.name}/SKILL.md has no frontmatter name")
        elif meta["name"] != d.name:
            add("skills", "frontmatter",
                f"skills/{d.name}/SKILL.md frontmatter name {meta['name'][:64]!r} != dir name")
        else:
            if meta["name"] in seen:
                add("skills", "duplicate",
                    f"skill name {meta['name'][:64]!r} also at {seen[meta['name']]}")
            seen[meta["name"]] = f"skills/{d.name}"
        if not meta.get("description"):
            add("skills", "frontmatter", f"skills/{d.name}/SKILL.md frontmatter has no description")
        steps = [int(s) for s in _STEPS_RE.findall(body)]
        if steps != list(range(1, len(steps) + 1)):
            add("skills", "steps", f"skills/{d.name}/SKILL.md steps are not numbered 1..N in order")
        if check_skill is not None:
            try:
                err = check_skill(d.name, body)
            except Exception:  # noqa: BLE001
                err = None
            if err:
                add("skills", "invalid", f"skills/{d.name}/SKILL.md: {str(err)[:160]}")
    return bodies


def _scan_env(bodies: list[tuple[str, str]], src: dict[str, str], add) -> None:
    blob = "\n".join(src.values())
    for name, body in bodies:
        for env in sorted(set(_ENV_RE.findall(body))):
            if env not in blob:
                add("env", "ungrounded", f"skills/{name}/SKILL.md names {env}, absent from avrone/*.py")


def _scan_wiring(src: dict[str, str], add) -> None:
    extra = src.get("tools_extra.py", "")
    if not extra:
        add("wiring", "missing", "avrone/tools_extra.py not found under the checkout")
        return
    for fname in sorted(src):
        for fn in _REGISTER_DEF_RE.findall(src[fname]):
            if fn not in extra:
                add("wiring", "unwired",
                    f"avrone/{fname} defines {fn} but tools_extra.py never calls it")


def _scan_caps(root: Path, add) -> None:
    ll = _read(root / "skills" / "load-limits" / "SKILL.md")
    try:
        from .tools_limits import _limits
        rows = _limits()
    except Exception:  # noqa: BLE001
        rows = []
    for row in rows:
        env = str(row[0])
        if env not in ll:
            add("caps", "unmapped", f"load cap {env} is not mapped in skills/load-limits")


def scan(root: Path) -> dict[str, Any]:
    """One bounded coherence scan of the checkout; findings are advisory, never auto-applied."""
    findings: list[dict[str, str]] = []

    def add(scope: str, kind: str, detail: str) -> None:
        findings.append({"scope": scope, "kind": kind, "detail": detail})

    src_dir = root / "avrone"
    py_files = sorted(p for p in src_dir.glob("*.py") if p.is_file())[:GAP_SCAN_MAX_FILES]
    src = {p.name: _read(p) for p in py_files}
    _scan_versions(root, src, add)
    bodies = _scan_skills(root, add)
    _scan_env(bodies, src, add)
    _scan_wiring(src, add)
    _scan_caps(root, add)
    by_scope: dict[str, int] = {}
    for f in findings:
        by_scope[f["scope"]] = by_scope.get(f["scope"], 0) + 1
    return {"root": str(root), "scanned_files": len(py_files), "findings": findings,
            "total": len(findings), "by_scope": by_scope}
