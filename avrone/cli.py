"""avrone-cli: run AVRONE headless from a terminal or a script.

    avrone-cli squad "build a todo app" [-n 4]
    avrone-cli chat  "what's in the workspace?"
    avrone-cli deep  "is X true?" [--first-answer TEXT | --first-answer-file PATH] [-n 4]
    avrone-cli tools
    avrone-cli runs [--limit 20]

The markdown answer (or deep report) goes to stdout; live event lines go to
stderr unless --quiet; --json PATH writes the full result as JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .bootstrap import build_core, run_cleanups
from .codex import Event
from .config import Settings


def _event_line(ev: Event) -> str:
    d = ev.data
    k = ev.kind
    if k == "run.start":
        detail = f"mode={d.get('mode')} size={d.get('squad_size', '-')}"
    elif k == "agent.spawn":
        detail = f"{d.get('role')}" + (f" (under {d['parent']})" if d.get("parent") else "")
    elif k == "tool.call":
        detail = f"{d.get('tool')} {json.dumps(d.get('args'), default=str)[:100]}"
    elif k == "tool.result":
        detail = str(d.get("output", "")).replace("\n", " ")[:100]
    elif k == "agent.done":
        detail = str(d.get("result", "")).replace("\n", " ")[:100]
    elif k == "lead.planned":
        detail = ", ".join(str(s.get("role")) for s in d.get("subtasks", []))
    elif k == "error":
        detail = str(d.get("message", ""))[:200]
    elif k == "run.done":
        detail = "done"
    else:
        detail = json.dumps(d, default=str)[:100]
    return f"[{ev.run_id}] {k:<14} {ev.agent:<18} {detail}"


async def _pump(q: asyncio.Queue[Event]) -> None:
    while True:
        ev = await q.get()
        print(_event_line(ev), file=sys.stderr, flush=True)


def _write_json(path: str | None, data: Any) -> None:
    if path:
        Path(path).write_text(json.dumps(data, indent=2, default=str))


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    if getattr(args, "max_steps", None):
        settings.max_steps = args.max_steps
    core, cleanups = await build_core(settings, extras=not args.no_extras)
    pump = None
    q = None
    try:
        if args.cmd == "tools":
            items = core.tools.list()
            _write_json(args.json, items)
            for t in items:
                print(f"{t['name']:<28} [{t['source']}] {t['args']}\n    {t['description']}")
            return 0
        if args.cmd == "runs":
            items = core.codex.runs(args.limit)
            _write_json(args.json, items)
            for r in items:
                print(f"{r['run_id']}  {str(r.get('prompt', ''))[:90]}")
            return 0

        if not args.quiet:
            q = core.codex.subscribe()
            pump = asyncio.create_task(_pump(q))
        if args.cmd == "squad":
            result = await core.squad(args.prompt, args.n)
            text = result["answer"]
        elif args.cmd == "chat":
            result = await core.chat(args.prompt)
            text = result["answer"]
        else:  # deep
            first = args.first_answer
            if args.first_answer_file:
                first = Path(args.first_answer_file).read_text()
            result = await core.deep(args.prompt, first, args.n)
            text = result["report"]
        await asyncio.sleep(0)  # let the pump print the last events
        _write_json(args.json, result)
        print(text)
        return 0
    finally:
        if pump:
            pump.cancel()
        if q is not None:
            core.codex.unsubscribe(q)
        await run_cleanups(cleanups)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="avrone-cli", description="Run AVRONE agents headless.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", metavar="PATH", help="write the full result as JSON to PATH")
    common.add_argument("--quiet", "-q", action="store_true", help="no live event lines on stderr")
    common.add_argument("--no-extras", action="store_true", help="skip extra tools, plugins and MCP servers")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("squad", parents=[common], help="parallel squad + lead merge")
    s.add_argument("prompt")
    s.add_argument("-n", type=int, default=None, help="squad size")
    s.add_argument("--max-steps", type=int, default=None)

    c = sub.add_parser("chat", parents=[common], help="one tool-using agent")
    c.add_argument("prompt")
    c.add_argument("--max-steps", type=int, default=None)

    d = sub.add_parser("deep", parents=[common], help="second-wave deep analysis of an answer")
    d.add_argument("prompt", help="the question")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--first-answer", metavar="TEXT", help="the answer to stress-test")
    g.add_argument("--first-answer-file", metavar="PATH", help="read the answer to stress-test from a file")
    d.add_argument("-n", type=int, default=None, help="number of reviewers (max 6)")
    d.add_argument("--max-steps", type=int, default=None)

    sub.add_parser("tools", parents=[common], help="list registered tools")
    r = sub.add_parser("runs", parents=[common], help="list recent runs")
    r.add_argument("--limit", type=int, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as e:
        print(f"avrone-cli: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
