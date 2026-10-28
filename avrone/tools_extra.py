"""Extra agent tooling: search, python, file navigation/editing, HTTP, git, memory,
artifacts, docker and a think scratchpad.

Usage:
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(registry)   # registry: avrone.tools.ToolRegistry

New options (all optional, read via env / getattr so config.py needs no change):
    AVRONE_SEARCH_PROVIDER  auto | duckduckgo | brave | tavily   (default auto)
    BRAVE_API_KEY           enables Brave Search API (auto picks it when set)
    TAVILY_API_KEY          enables Tavily Search API (auto picks it when Brave is not set)
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .tools import Tool, _safe_path

if TYPE_CHECKING:
    from .trinity import AgentContext

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
USER_AGENT = "Mozilla/5.0 (compatible; AVRONE/0.2)"

# Tests (or embedders) may set this to an httpx transport, e.g. httpx.MockTransport.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _client(timeout: float = 30) -> httpx.AsyncClient:
    kw: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if HTTP_TRANSPORT is not None:
        kw["transport"] = HTTP_TRANSPORT
    return httpx.AsyncClient(**kw)


def _setting(ctx: "AgentContext", name: str, default: Any) -> Any:
    return getattr(ctx.core.settings, name, default)


def _ws(ctx: "AgentContext") -> Path:
    return Path(ctx.core.runner.workspace).resolve()


def _rel(ctx: "AgentContext", p: Path) -> str:
    try:
        return str(p.resolve().relative_to(_ws(ctx))) or "."
    except ValueError:
        return str(p)


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    codex = getattr(ctx.core, "codex", None)
    if codex is not None:
        try:
            codex.emit(ctx.run_id, kind, ctx.name, **data)
        except Exception:
            pass


# --------------------------------------------------------------------------- web_search

def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _ddg_url(href: str) -> str:
    href = _html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    u = urlparse(href)
    if "duckduckgo.com" in u.netloc and u.path.startswith("/l/"):
        q = parse_qs(u.query).get("uddg")
        if q:
            return unquote(q[0])
    return href


def parse_ddg_html(text: str, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    links = list(re.finditer(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', text, re.S))
    for i, m in enumerate(links):
        tag = m.group(0)
        href_m = re.search(r'href="([^"]+)"', tag)
        if not href_m:
            continue
        url = _ddg_url(href_m.group(1))
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        end = links[i + 1].start() if i + 1 < len(links) else len(text)
        seg = text[m.end():end]
        sn = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', seg, re.S)
        results.append({"title": _strip_tags(m.group(1)), "url": url,
                        "snippet": _strip_tags(sn.group(1)) if sn else ""})
        if len(results) >= max_results:
            break
    return results


async def _search_ddg(q: str, n: int) -> list[dict[str, str]]:
    async with _client() as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": q},
                         headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return parse_ddg_html(r.text, n)


async def _search_brave(q: str, n: int, key: str) -> list[dict[str, str]]:
    async with _client() as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": q, "count": n},
                        headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    items = (r.json().get("web") or {}).get("results") or []
    return [{"title": _strip_tags(i.get("title", "")), "url": i.get("url", ""),
             "snippet": _strip_tags(i.get("description", ""))} for i in items[:n]]


async def _search_tavily(q: str, n: int, key: str) -> list[dict[str, str]]:
    async with _client() as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": q, "max_results": n},
                         headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    items = r.json().get("results") or []
    return [{"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": (i.get("content") or "")[:400]} for i in items[:n]]


async def t_web_search(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "allow_network", True):
        return "ERROR: network access is disabled."
    q = str(args.get("query", "")).strip()
    if not q:
        return "ERROR: query is required"
    n = _int(args.get("max_results"), 5, 1, 20)
    provider = str(_setting(ctx, "search_provider", "") or os.getenv("AVRONE_SEARCH_PROVIDER", "auto")).lower()
    brave = str(_setting(ctx, "brave_api_key", "") or os.getenv("BRAVE_API_KEY", ""))
    tavily = str(_setting(ctx, "tavily_api_key", "") or os.getenv("TAVILY_API_KEY", ""))
    if provider == "auto":
        provider = "brave" if brave else "tavily" if tavily else "duckduckgo"
    if provider == "brave":
        if not brave:
            return "ERROR: AVRONE_SEARCH_PROVIDER=brave but BRAVE_API_KEY is not set"
        results = await _search_brave(q, n, brave)
    elif provider == "tavily":
        if not tavily:
            return "ERROR: AVRONE_SEARCH_PROVIDER=tavily but TAVILY_API_KEY is not set"
        results = await _search_tavily(q, n, tavily)
    else:
        provider = "duckduckgo"
        results = await _search_ddg(q, n)
    if not results:
        return f"No results for {q!r} ({provider})."
    lines = [f"Search results for {q!r} ({provider}):"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}" + (f"\n   {r['snippet']}" if r["snippet"] else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- python

async def t_python(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "agents_can_shell", True):
        return "ERROR: code execution is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    code = str(args.get("code", ""))
    if not code.strip():
        return "ERROR: code is required"
    tmp = _ws(ctx) / ".avrone" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"snippet_{uuid.uuid4().hex[:10]}.py"
    f.write_text(code)
    try:
        res = await ctx.core.runner.run(f"python3 {shlex.quote(_rel(ctx, f))}")
    finally:
        f.unlink(missing_ok=True)
    return res.text()


# --------------------------------------------------------------------------- list_files / search_files

async def t_list_files(ctx: "AgentContext", args: dict[str, Any]) -> str:
    ws = _ws(ctx)
    root = _safe_path(ws, str(args.get("path", "") or "."))
    if not root.exists():
        return f"ERROR: {args.get('path')} not found"
    if root.is_file():
        return f"{_rel(ctx, root)} ({root.stat().st_size} bytes)"
    depth = _int(args.get("depth"), 3, 1, 10)
    cap = 400
    lines = [_rel(ctx, root) + "/"]
    count = 0
    truncated = False

    def walk(d: Path, level: int, prefix: str) -> None:
        nonlocal count, truncated
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP_DIRS:
                continue
            if count >= cap:
                truncated = True
                return
            count += 1
            if e.is_dir() and not e.is_symlink():
                lines.append(f"{prefix}{e.name}/")
                if level < depth:
                    walk(e, level + 1, prefix + "  ")
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{e.name} ({size}b)")

    walk(root, 1, "  ")
    if truncated:
        lines.append(f"...[truncated at {cap} entries]")
    return "\n".join(lines)


async def t_search_files(ctx: "AgentContext", args: dict[str, Any]) -> str:
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return "ERROR: pattern is required"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    ws = _ws(ctx)
    root = _safe_path(ws, str(args.get("path", "") or "."))
    if not root.exists():
        return f"ERROR: {args.get('path')} not found"
    cap, hits, scanned = 200, [], 0
    files = [root] if root.is_file() else _iter_files(root)
    for f in files:
        if scanned > 5000 or len(hits) >= cap:
            break
        scanned += 1
        try:
            if f.stat().st_size > 1_000_000:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for no, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{_rel(ctx, f)}:{no}: {line.strip()[:300]}")
                if len(hits) >= cap:
                    break
    if not hits:
        return f"No matches for /{pattern}/."
    out = "\n".join(hits)
    if len(hits) >= cap:
        out += f"\n...[capped at {cap} matches]"
    return out


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


# --------------------------------------------------------------------------- edit_file

async def t_edit_file(ctx: "AgentContext", args: dict[str, Any]) -> str:
    p = _safe_path(_ws(ctx), str(args.get("path", "")))
    if not p.is_file():
        return f"ERROR: {args.get('path')} not found"
    old, new = str(args.get("old", "")), str(args.get("new", ""))
    if not old:
        return "ERROR: 'old' must be a non-empty string"
    text = p.read_text(errors="replace")
    n = text.count(old)
    if n == 0:
        return f"ERROR: 'old' text not found in {_rel(ctx, p)}"
    if n > 1:
        return f"ERROR: 'old' text matches {n} times in {_rel(ctx, p)}; include more context to make it unique"
    p.write_text(text.replace(old, new, 1))
    return f"edited {_rel(ctx, p)} (1 replacement)"


# --------------------------------------------------------------------------- http_request

_KEY_HEADERS = ("content-type", "content-length", "location", "etag", "last-modified",
                "cache-control", "retry-after", "x-ratelimit-remaining")


async def t_http_request(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "allow_network", True):
        return "ERROR: network access is disabled."
    method = str(args.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return f"ERROR: unsupported method {method}"
    url = str(args.get("url", ""))
    if not re.match(r"^https?://", url):
        return "ERROR: url must start with http(s)://"
    headers = args.get("headers") or {}
    if not isinstance(headers, dict):
        return "ERROR: headers must be an object"
    headers = {str(k): str(v) for k, v in headers.items()}
    headers.setdefault("User-Agent", "AVRONE/0.2")
    body = args.get("body")
    kw: dict[str, Any] = {"headers": headers}
    if isinstance(body, (dict, list)):
        kw["json"] = body
    elif body is not None:
        kw["content"] = str(body).encode()
    async with _client() as c:
        r = await c.request(method, url, **kw)
    lines = [f"HTTP {r.status_code} {r.reason_phrase}"]
    for h in _KEY_HEADERS:
        if h in r.headers:
            lines.append(f"{h}: {r.headers[h]}")
    text = r.text
    limit = 8000
    lines.append("")
    lines.append(text[:limit] + (f"\n...[truncated, {len(text)} chars total]" if len(text) > limit else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- git

def _is_force_push(argv: list[str]) -> bool:
    if "push" not in argv:
        return False
    after = argv[argv.index("push") + 1:]
    for a in after:
        if a in ("-f", "--force") or a.startswith("--force") or a == "--mirror":
            return True
        if a.startswith("-") and not a.startswith("--") and "f" in a[1:]:
            return True
        if a.startswith("+"):
            return True
    return False


async def t_git(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "agents_can_shell", True):
        return "ERROR: git is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    raw = str(args.get("args", "")).strip()
    if raw.startswith("git "):
        raw = raw[4:]
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        return f"ERROR: could not parse args: {e}"
    if not argv:
        return "ERROR: args is required, e.g. \"status\""
    if _is_force_push(argv):
        return "ERROR: force push is not allowed."
    return (await ctx.core.runner.run("git " + shlex.join(argv))).text()


# --------------------------------------------------------------------------- memory (Eternal CodeX)

def _mem_file(ctx: "AgentContext") -> Path:
    d = Path(_setting(ctx, "data_dir", "./data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.jsonl"


def load_memory(path: Path) -> dict[str, dict[str, Any]]:
    """Latest record per key."""
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "key" in rec:
            latest[str(rec["key"])] = rec
    return latest


async def t_memory_save(ctx: "AgentContext", args: dict[str, Any]) -> str:
    key = str(args.get("key", "")).strip()
    if not key:
        return "ERROR: key is required"
    value = str(args.get("value", ""))
    rec = {"key": key, "value": value, "ts": time.time(), "run_id": ctx.run_id, "agent": ctx.name}
    with _mem_file(ctx).open("a") as f:
        f.write(json.dumps(rec) + "\n")
    _emit(ctx, "memory.save", key=key, bytes=len(value.encode()))
    return f"saved memory '{key}'"


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t}


async def t_memory_recall(ctx: "AgentContext", args: dict[str, Any]) -> str:
    mem = load_memory(_mem_file(ctx))
    if not mem:
        return "No memories saved yet."
    query = str(args.get("query", "") or "").strip()
    recs = list(mem.values())
    if query:
        qt = _tokens(query)
        ql = query.lower()
        scored = []
        for r in recs:
            k, v = str(r["key"]).lower(), str(r.get("value", "")).lower()
            score = 3 * len(qt & _tokens(k)) + len(qt & _tokens(v))
            if ql in k:
                score += 5
            elif ql in v:
                score += 2
            if score > 0:
                scored.append((score, r.get("ts", 0), r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        recs = [r for _, _, r in scored]
        if not recs:
            return f"No memories match {query!r}."
    else:
        recs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    out = [f"- {r['key']}: {str(r.get('value', ''))[:1000]}" for r in recs[:20]]
    return "\n".join(out)


# --------------------------------------------------------------------------- artifact_save

async def t_artifact_save(ctx: "AgentContext", args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "ERROR: name is required"
    run = re.sub(r"[^A-Za-z0-9_\-.]", "_", str(ctx.run_id or "norun")) or "norun"
    base = _safe_path(_ws(ctx), f"artifacts/{run}")
    p = _safe_path(base, name)
    if p == base:
        return "ERROR: bad artifact name"
    content = str(args.get("content", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    rel = _rel(ctx, p)
    size = p.stat().st_size
    _emit(ctx, "artifact", path=rel, bytes=size)
    return rel


# --------------------------------------------------------------------------- docker_run

async def t_docker_run(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if not _setting(ctx, "agents_can_shell", True):
        return "ERROR: docker is disabled for agents (AVRONE_AGENTS_CAN_SHELL=false)."
    if not shutil.which("docker"):
        return "ERROR: docker is not available on this host (no `docker` binary found)."
    image = str(args.get("image", "")).strip()
    cmd = str(args.get("cmd", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-/:@]*", image):
        return "ERROR: invalid image name"
    if not cmd.strip():
        return "ERROR: cmd is required"
    ws = _ws(ctx)
    argv = ["docker", "run", "--rm", "-v", f"{ws}:/workspace", "-w", "/workspace"]
    if not _setting(ctx, "allow_network", True):
        argv += ["--network", "none"]
    argv += [image, "sh", "-c", cmd]
    timeout = int(_setting(ctx, "command_timeout", 120)) + 180  # allow for image pull
    res = await ctx.core.runner._exec(argv, f"docker run --rm {image} {cmd}", timeout)
    return res.text()


# --------------------------------------------------------------------------- think

async def t_think(ctx: "AgentContext", args: dict[str, Any]) -> str:
    _emit(ctx, "agent.think", thought=str(args.get("thought", ""))[:4000], tool="think")
    return "noted"


# --------------------------------------------------------------------------- registration

EXTRA_TOOLS: list[tuple[str, str, str, Any]] = [
    ("web_search", "search the web (DuckDuckGo by default; Brave/Tavily if API keys set); returns numbered title/url/snippet results.",
     '{"query": str, "max_results"?: int}', t_web_search),
    ("python", "run a Python 3 snippet in the workspace and return its stdout/stderr.",
     '{"code": str}', t_python),
    ("list_files", "show a directory tree of the workspace (skips .git, node_modules, __pycache__).",
     '{"path"?: str, "depth"?: int}', t_list_files),
    ("search_files", "regex search across workspace text files; returns file:line: match.",
     '{"pattern": str, "path"?: str}', t_search_files),
    ("edit_file", "replace one exact, unique occurrence of `old` with `new` in a workspace file.",
     '{"path": str, "old": str, "new": str}', t_edit_file),
    ("http_request", "make an HTTP request (any method) and return status, key headers and body.",
     '{"method": str, "url": str, "headers"?: dict, "body"?: str|dict}', t_http_request),
    ("git", "run a git command in the workspace, e.g. {\"args\": \"status\"} (force push refused).",
     '{"args": str}', t_git),
    ("memory_save", "save a fact to persistent agent memory (Eternal CodeX); latest value per key wins.",
     '{"key": str, "value": str}', t_memory_save),
    ("memory_recall", "recall persistent memories, ranked by keyword match (all recent if no query).",
     '{"query"?: str}', t_memory_recall),
    ("artifact_save", "save a deliverable file under artifacts/<run_id>/ and announce it; returns its path.",
     '{"name": str, "content": str}', t_artifact_save),
    ("docker_run", "run a command in a fresh container of the given image with the workspace mounted at /workspace.",
     '{"image": str, "cmd": str}', t_docker_run),
    ("think", "private scratchpad: record your reasoning/plan; no side effects.",
     '{"thought": str}', t_think),
]


def register_extra_tools(registry) -> None:
    """Add every extra tool to `registry` (an avrone.tools.ToolRegistry)."""
    for name, desc, args_doc, fn in EXTRA_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
    from .netdiag import register_net_tools
    register_net_tools(registry)
    from .agentkit import register_kit_tools
    register_kit_tools(registry)
