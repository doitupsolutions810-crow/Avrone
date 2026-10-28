"""Agent kit (v0.8): richer tooling plus better handling of tasks, resources and skills.

Tools
  image_inspect  size, mode, colors, entropy and an estimated module grid of an image
  image_decode   decode QR / DataMatrix / Aztec / PDF417 / 1D barcodes (zxing-cpp)
  skill_list     list skills (markdown playbooks in AVRONE_SKILLS_DIR, default ./skills)
  skill_load     load one skill's instructions into the agent's context
  env_install    install a package into the agent's environment (pip, or apt with sudo fallback);
                 every change is recorded in the resource ledger
  resources      the resource ledger: what each agent installed or changed, when, and the outcome
  task_board     shared per-run board: add / update / list subtasks with status and owner

Self-changes policy (AVRONE_SELF_CHANGES): allow (default) | log | deny.
  allow and log both execute and record; deny refuses env_install.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool, _safe_path

if TYPE_CHECKING:
    from .trinity import AgentContext

PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,80}(==[A-Za-z0-9.*+\-]{1,40})?$")


def _ws(ctx: "AgentContext") -> Path:
    return Path(ctx.core.runner.workspace).resolve()


def _data(ctx: "AgentContext") -> Path:
    d = Path(ctx.core.settings.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


def _img_path(ctx: "AgentContext", p: str) -> Path:
    path = Path(p)
    if path.is_absolute() and path.exists() and os.getenv("AVRONE_ALLOW_ABS_READ", "1") == "1":
        return path
    return _safe_path(_ws(ctx), p)


# ---------------------------------------------------------------- images
async def t_image_inspect(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from PIL import Image
    p = _img_path(ctx, str(args["path"]))
    im = Image.open(p)
    g = im.convert("L")
    hist = g.histogram()
    total = sum(hist)
    ent = -sum((h / total) * math.log2(h / total) for h in hist if h)
    colors = im.convert("RGB").getcolors(maxcolors=1 << 16)
    top = sorted(colors, reverse=True)[:6] if colors else []
    # estimate module size from run lengths along the middle row (barcode-ish images)
    bw = g.point(lambda v: 255 if v > 127 else 0)
    w, h = bw.size
    runs: Counter[int] = Counter()
    for y in (h // 3, h // 2, 2 * h // 3):
        row = [bw.getpixel((x, y)) for x in range(w)]
        n = 1
        for a, b in zip(row, row[1:]):
            if a == b:
                n += 1
            else:
                runs[n] += 1
                n = 1
    unit = min((r for r, c in runs.most_common(8) if r >= 2), default=None)
    grid = f"~{round(w / unit)}x{round(h / unit)} modules (unit {unit}px)" if unit else "n/a"
    return (f"{p.name}: {w}x{h} {im.mode} format={im.format} size={p.stat().st_size}B\n"
            f"entropy={ent:.3f} bits/px  dark_ratio={sum(hist[:128]) / total:.3f}\n"
            f"top colors: {[(c, rgb) for c, rgb in top]}\nestimated grid: {grid}")


async def t_image_decode(ctx: "AgentContext", args: dict[str, Any]) -> str:
    try:
        import zxingcpp
    except ImportError:
        return ("ERROR: zxing-cpp not installed. It is a pip package (not apt): "
                "env_install {\"package\": \"zxing-cpp\", \"manager\": \"pip\"} then retry.")
    from PIL import Image
    p = _img_path(ctx, str(args["path"]))
    im = Image.open(p).convert("L")
    seen: dict[tuple[str, str], str] = {}
    for scale in (1.0, 0.5, 2.0, 0.25):
        s = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.NEAREST)
        for r in zxingcpp.read_barcodes(s, try_rotate=True, try_downscale=True):
            key = (str(r.format), r.text)
            if key not in seen:
                pos = r.position
                extra = getattr(r, "extra", None) or {}
                ver = f" version={extra['Version']}" if isinstance(extra, dict) and extra.get("Version") else ""
                seen[key] = (f"- {r.format.name}{ver} valid={r.valid} ec={getattr(r, 'ec_level', '?')} "
                             f"scale={scale} at ({pos.top_left.x},{pos.top_left.y})-({pos.bottom_right.x},{pos.bottom_right.y})"
                             f"\n  text[{len(r.text)}]: {r.text[:4000]}")
    _emit(ctx, "image.decode", path=str(p), found=len(seen))
    return f"{len(seen)} symbol(s) in {p.name}\n" + "\n".join(seen.values()) if seen else f"no symbols decoded in {p.name}"


# ---------------------------------------------------------------- skills
def _skills_dir(ctx: "AgentContext") -> Path:
    return Path(os.getenv("AVRONE_SKILLS_DIR", str(Path(__file__).resolve().parent.parent / "skills")))


def _skills(ctx: "AgentContext") -> dict[str, tuple[str, Path]]:
    out: dict[str, tuple[str, Path]] = {}
    d = _skills_dir(ctx)
    for f in sorted(d.glob("*/SKILL.md")) + sorted(d.glob("*.md")):
        name = f.parent.name if f.name == "SKILL.md" else f.stem
        text = f.read_text(errors="replace")
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        desc = m.group(1).strip() if m else text.strip().splitlines()[0][:120]
        out[name] = (desc, f)
    return out


async def t_skill_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    sk = _skills(ctx)
    return "\n".join(f"- {n}: {d}" for n, (d, _) in sk.items()) or "no skills installed"


async def t_skill_load(ctx: "AgentContext", args: dict[str, Any]) -> str:
    sk = _skills(ctx)
    name = str(args["name"])
    if name not in sk:
        return f"ERROR: no skill {name!r}. Available: {', '.join(sk) or 'none'}"
    _emit(ctx, "skill.load", skill=name)
    return sk[name][1].read_text(errors="replace")[:12000]


# ---------------------------------------------------------------- resources
def _ledger(ctx: "AgentContext") -> Path:
    return _data(ctx) / "resources.jsonl"


def record_change(ctx: "AgentContext", action: str, target: str, ok: bool, detail: str = "") -> None:
    entry = {"ts": time.time(), "run_id": ctx.run_id, "agent": ctx.name, "action": action, "target": target,
             "ok": ok, "detail": detail[:500]}
    with _ledger(ctx).open("a") as f:
        f.write(json.dumps(entry) + "\n")
    _emit(ctx, "env.change", **{k: v for k, v in entry.items() if k not in ("run_id", "agent")})


async def t_env_install(ctx: "AgentContext", args: dict[str, Any]) -> str:
    policy = os.getenv("AVRONE_SELF_CHANGES", "allow").lower()
    pkg, mgr = str(args["package"]).strip(), str(args.get("manager", "pip")).lower()
    if not PKG_RE.match(pkg):
        return "ERROR: invalid package name"
    if policy == "deny":
        record_change(ctx, f"{mgr} install (denied)", pkg, False, "AVRONE_SELF_CHANGES=deny")
        return "ERROR: self-changes are disabled on this node (AVRONE_SELF_CHANGES=deny)"
    prior = _installed_before(ctx, mgr, pkg)
    if prior:
        return f"{pkg} already installed via {mgr} by {prior} (resource ledger); no change made"
    lock = _data(ctx) / f".install-{mgr}.lock"
    with open(lock, "w") as lf:  # one installer per package manager per node: no apt/dpkg lock fights
        import fcntl
        await asyncio.to_thread(fcntl.flock, lf, fcntl.LOCK_EX)
        try:
            prior = _installed_before(ctx, mgr, pkg)
            if prior:
                return f"{pkg} already installed via {mgr} by {prior} (resource ledger); no change made"
            return await _do_install(ctx, mgr, pkg)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _installed_before(ctx: "AgentContext", mgr: str, pkg: str) -> str | None:
    p = _ledger(ctx)
    if not p.exists():
        return None
    for line in reversed(p.read_text().splitlines()):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ok") and r.get("action") == f"{mgr} install" and r.get("target") == pkg:
            return r.get("agent", "another agent")
    return None


async def _do_install(ctx: "AgentContext", mgr: str, pkg: str) -> str:
    if mgr == "pip":
        cmd = f"python3 -m pip install -q {pkg} 2>&1 | tail -n 3"
    elif mgr == "apt":
        cmd = (f"(apt-get install -y -q {pkg} || sudo -n apt-get install -y -q {pkg} || "
               f"(sudo -n apt-get update -q && sudo -n apt-get install -y -q {pkg})) 2>&1 | tail -n 3")
    else:
        return "ERROR: manager must be pip or apt"
    res = await ctx.core.runner.run(cmd, timeout=600)
    check = await ctx.core.runner.run(
        f"python3 -m pip show {pkg.split('==')[0]} >/dev/null 2>&1" if mgr == "pip" else f"dpkg -s {pkg} >/dev/null 2>&1",
        timeout=30)
    ok = check.exit_code == 0
    record_change(ctx, f"{mgr} install", pkg, ok, (res.stdout or res.stderr)[-300:])
    return f"{'installed' if ok else 'FAILED'} {pkg} via {mgr} (logged to resource ledger)\n{res.text(1500)}"


async def t_resources(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _ledger(ctx)
    if not p.exists():
        return "resource ledger empty"
    rows = [json.loads(line) for line in p.read_text().splitlines()[-int(args.get("limit", 30)):]]
    return "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['agent']} {r['action']} {r['target']} "
                     f"{'ok' if r['ok'] else 'FAIL'}" for r in rows)


# ---------------------------------------------------------------- task board
def _board(ctx: "AgentContext") -> Path:
    d = _data(ctx) / "boards"
    d.mkdir(exist_ok=True)
    return d / f"{re.sub(r'[^A-Za-z0-9_-]', '', ctx.run_id) or 'run'}.json"


async def t_task_board(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _board(ctx)
    board: list[dict[str, Any]] = json.loads(p.read_text()) if p.exists() else []
    op = str(args.get("op", "list"))
    if op == "add":
        item = {"id": len(board) + 1, "title": str(args["title"])[:200], "status": "todo",
                "owner": str(args.get("owner", ctx.name)), "note": "", "by": ctx.name}
        board.append(item)
    elif op == "update":
        item = next((t for t in board if t["id"] == int(args["id"])), None)
        if not item:
            return "ERROR: no such task"
        for k in ("status", "note", "owner"):
            if k in args:
                item[k] = str(args[k])[:1000]
    elif op != "list":
        return "ERROR: op must be add, update or list"
    p.write_text(json.dumps(board, indent=1))
    if op != "list":
        _emit(ctx, "task.board", op=op, board=board[-10:])
    return "\n".join(f"#{t['id']} [{t['status']}] {t['title']} (owner {t['owner']}){' - ' + t['note'] if t['note'] else ''}"
                     for t in board) or "board empty"


KIT_TOOLS = [
    ("image_inspect", "inspect an image: size, mode, entropy, dominant colors, estimated barcode module grid.",
     '{"path": str}', t_image_inspect),
    ("image_decode", "decode every QR/DataMatrix/Aztec/PDF417/1D barcode in an image at several scales.",
     '{"path": str}', t_image_decode),
    ("skill_list", "list available skills (playbooks for common jobs).", "{}", t_skill_list),
    ("skill_load", "load a skill's step-by-step playbook before starting that kind of job.", '{"name": str}', t_skill_load),
    ("env_install", "install a missing package into your environment (pip or apt); logged to the resource ledger.",
     '{"package": str, "manager"?: "pip"|"apt"}', t_env_install),
    ("resources", "show the resource ledger: packages/changes agents made to their environments.",
     '{"limit"?: int}', t_resources),
    ("task_board", "shared board for this run: op=add {title, owner?} | update {id, status?, note?, owner?} | list.",
     '{"op": "add"|"update"|"list", "title"?: str, "id"?: int, "status"?: str, "note"?: str, "owner"?: str}',
     t_task_board),
]


def register_kit_tools(registry) -> None:
    for name, desc, args_doc, fn in KIT_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
