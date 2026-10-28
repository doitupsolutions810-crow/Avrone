"""Fleet: link AVRONE servers into a network of nodes that spawn agents on each other.

Every AVRONE server can be a node, a router (a node with children), or both.
  - A node joins a parent with AVRONE_FLEET_PARENT=https://parent:8000 and keeps a heartbeat.
  - Agents get tools to list nodes and spawn squads/chats/deep runs on a remote node.
  - The remote node's agents have the same tools, so they spawn and link further down.
  - Routing: a target like "router1/edge7" is forwarded hop by hop through router1.

Security (required, no token = fleet off):
  AVRONE_FLEET_TOKEN   shared secret. Every fleet request is HMAC-SHA256 signed over
                       method, path, timestamp, nonce and body hash; 60 s window; nonces
                       are remembered so a captured request can't be replayed.
  AVRONE_FLEET_MAX_HOPS  loop/recursion guard across the fleet (default 3)
  AVRONE_FLEET_ALLOW     optional comma list of node ids this server will accept / spawn on
Other env:
  AVRONE_NODE_ID        stable id for this server (default: hostname-derived, persisted)
  AVRONE_PUBLIC_URL     URL others use to reach this node (sent when registering)
  AVRONE_FLEET_PARENT   parent/router to join
  AVRONE_FLEET_HEARTBEAT seconds between heartbeats (default 15)
Run nodes over HTTPS or a private mesh (Tailscale/WireGuard); the HMAC protects
integrity and auth, not confidentiality.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext, TrinityCore

log = logging.getLogger("avrone.fleet")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
WINDOW_S = 60

# Tests may set this to an httpx transport (e.g. ASGITransport routing to in-process apps).
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def sign(token: str, method: str, path: str, body: bytes, ts: str | None = None, nonce: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    nonce = nonce or secrets.token_hex(12)
    msg = "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()
    sig = hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()
    return {"x-avrone-ts": ts, "x-avrone-nonce": nonce, "x-avrone-sig": sig}


class FleetManager:
    def __init__(self, core: "TrinityCore", data_dir: Path):
        self.core = core
        self.token = _env("AVRONE_FLEET_TOKEN")
        self.max_hops = int(_env("AVRONE_FLEET_MAX_HOPS", "3") or 3)
        self.allow = {a.strip() for a in _env("AVRONE_FLEET_ALLOW").split(",") if a.strip()}
        self.parent = _env("AVRONE_FLEET_PARENT").rstrip("/")
        self.public_url = _env("AVRONE_PUBLIC_URL").rstrip("/")
        self.heartbeat_s = float(_env("AVRONE_FLEET_HEARTBEAT", "15") or 15)
        self.path = Path(data_dir) / "fleet.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        nid = _env("AVRONE_NODE_ID") or saved.get("node_id") or re.sub(
            r"[^a-z0-9-]", "-", socket.gethostname().lower())[:40] + "-" + secrets.token_hex(3)
        if not ID_RE.match(nid):
            raise ValueError(f"AVRONE_NODE_ID {nid!r} must match {ID_RE.pattern}")
        self.node_id = nid
        self.nodes: dict[str, dict[str, Any]] = saved.get("nodes", {})
        self._nonces: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self.queue = None  # JobQueue, attached by start_queue()
        self.worker = None
        self._save()

    # ------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _save(self) -> None:
        self.path.write_text(json.dumps({"node_id": self.node_id, "nodes": self.nodes}, indent=2))

    def status_of(self, n: dict[str, Any]) -> str:
        age = time.time() - n.get("last_seen", 0)
        if age < 3 * self.heartbeat_s:
            return "online"
        if age < 10 * self.heartbeat_s:
            return "degraded"
        return "offline"

    def list_nodes(self) -> list[dict[str, Any]]:
        return [{**{k: v for k, v in n.items()}, "status": self.status_of(n)} for n in self.nodes.values()]

    def info(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "enabled": self.enabled, "parent": self.parent or None,
                "public_url": self.public_url or None, "max_hops": self.max_hops,
                "children": len(self.nodes), "active_agents": sum(
                    1 for a in self.core.active.values() if a.get("status") == "working")}

    # ------------------------------------------------------------ auth
    def verify(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        if not self.enabled:
            raise PermissionError("fleet disabled: set AVRONE_FLEET_TOKEN")
        ts, nonce, sig = (headers.get("x-avrone-ts", ""), headers.get("x-avrone-nonce", ""),
                          headers.get("x-avrone-sig", ""))
        try:
            skew = abs(time.time() - int(ts))
        except ValueError:
            raise PermissionError("bad timestamp") from None
        if skew > WINDOW_S:
            raise PermissionError("stale request")
        expect = sign(self.token, method, path, body, ts, nonce)["x-avrone-sig"]
        if not hmac.compare_digest(expect, sig):
            raise PermissionError("bad signature")
        now = time.time()
        self._nonces = {k: v for k, v in self._nonces.items() if now - v < 2 * WINDOW_S}
        if nonce in self._nonces:
            raise PermissionError("replayed request")
        self._nonces[nonce] = now

    def allowed(self, node_id: str) -> bool:
        return not self.allow or node_id in self.allow

    # ------------------------------------------------------------ registry (server side)
    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        nid = str(payload.get("node_id", ""))
        url = str(payload.get("url", "")).rstrip("/")
        if not ID_RE.match(nid):
            raise ValueError("invalid node_id")
        if nid == self.node_id:
            raise ValueError("node_id collides with this server")
        if not self.allowed(nid):
            raise PermissionError(f"node {nid} not in AVRONE_FLEET_ALLOW")
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        existing = self.nodes.get(nid)
        if existing and existing.get("url") != url and self.status_of(existing) == "online":
            raise ValueError(f"node_id {nid} already online at another url")
        self.nodes[nid] = {"node_id": nid, "url": url, "last_seen": time.time(),
                           "capabilities": payload.get("capabilities", {}),
                           "children": payload.get("children", []),
                           "load": payload.get("load", 0),
                           "presence": payload.get("presence")}
        self._save()
        self.core.codex.emit("fleet", "fleet.join", self.node_id, node=nid, url=url)
        return {"ok": True, "parent": self.node_id}

    # ------------------------------------------------------------ client side
    def _client(self, timeout: float) -> httpx.AsyncClient:
        kw: dict[str, Any] = {"timeout": timeout}
        if HTTP_TRANSPORT is not None:
            kw["transport"] = HTTP_TRANSPORT
        return httpx.AsyncClient(**kw)

    async def _post(self, base: str, path: str, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        headers = {"content-type": "application/json", **sign(self.token, "POST", path, body)}
        async with self._client(timeout) as c:
            r = await c.post(base + path, content=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"{base}{path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def join_parent_once(self) -> dict[str, Any]:
        from .presence import snapshot
        payload = {"presence": await snapshot(self.core),"node_id": self.node_id, "url": self.public_url,
                   "capabilities": {"tools": self.core.tools.names(), "runner": self.core.runner.kind},
                   "children": sorted(self.nodes), "load": self.info()["active_agents"]}
        return await self._post(self.parent, "/api/fleet/register", payload, timeout=10)

    async def heartbeat_loop(self) -> None:
        backoff = self.heartbeat_s
        while True:
            try:
                await self.join_parent_once()
                backoff = self.heartbeat_s
            except Exception as e:  # noqa: BLE001
                log.warning("fleet heartbeat to %s failed: %s", self.parent, e)
                backoff = min(backoff * 2, 300)
            await asyncio.sleep(backoff)

    def start(self) -> None:
        if self.enabled and self.parent and self.public_url and self._task is None:
            self._task = asyncio.create_task(self.heartbeat_loop())

    def start_queue(self, data_dir: Path) -> None:
        from .jobqueue import JobQueue, Worker
        self.queue = JobQueue(Path(data_dir) / "jobs.db")
        self.worker = Worker(self.queue, self.run_job)
        self.worker.start()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self.worker:
            await self.worker.stop()
        if self.queue:
            await self.queue.close()
        for t in list(self._bg):
            t.cancel()

    def pick(self, target: str) -> dict[str, Any]:
        if target in ("", "auto"):
            live = [n for n in self.nodes.values() if self.status_of(n) == "online" and self.allowed(n["node_id"])]
            if not live:
                raise RuntimeError("no online nodes")
            return min(live, key=lambda n: n.get("load", 0))
        n = self.nodes.get(target)
        if not n:
            raise RuntimeError(f"unknown node {target!r}; known: {sorted(self.nodes) or 'none'}")
        if not self.allowed(target):
            raise PermissionError(f"node {target} not in AVRONE_FLEET_ALLOW")
        if self.status_of(n) == "offline":
            raise RuntimeError(f"node {target} is offline")
        return n

    async def dispatch(self, route: list[str], mode: str, prompt: str, hops: int, n: int | None = None,
                       origin_run: str = "") -> dict[str, Any]:
        """Send a job down `route` (first element = one of my children)."""
        if hops >= self.max_hops:
            raise PermissionError(f"max fleet hops ({self.max_hops}) reached")
        node = self.pick(route[0] if route else "auto")
        payload = {"route": route[1:], "mode": mode, "prompt": prompt, "n": n, "hops": hops + 1,
                   "from": self.node_id, "origin_run": origin_run}
        node["load"] = node.get("load", 0) + 1
        try:
            return await self._post(node["url"], "/api/fleet/run", payload, timeout=900)
        finally:
            node["load"] = max(0, node.get("load", 1) - 1)

    async def send_message(self, route: list[str], agent: str, text: str, sender: str, hops: int = 0,
                           wait: bool = False) -> dict[str, Any]:
        """Deliver `text` to `agent` on the node at `route` (empty route = this node)."""
        if route:
            if hops >= self.max_hops:
                raise PermissionError(f"max fleet hops ({self.max_hops}) reached")
            node = self.pick(route[0])
            payload = {"route": route[1:], "agent": agent, "message": text, "from": sender, "hops": hops + 1, "wait": wait}
            res = await self._post(node["url"], "/api/fleet/message", payload, timeout=900 if wait else 30)
            return {**res, "path": [self.node_id] + res.get("path", [])}
        return await self.deliver_local(agent, text, sender, wait)

    async def deliver_local(self, agent: str, text: str, sender: str, wait: bool) -> dict[str, Any]:
        c = self.core
        if c.message_agent(agent, text, sender):
            return {"node": self.node_id, "path": [self.node_id], "agent": agent, "delivered": "live",
                    "note": "queued in the live agent's inbox; it acts on it at its next step"}
        # Agent finished or unknown here: start a fresh agent with that role in this node's environment.
        role = agent.rsplit("-", 1)[0] or "Agent"
        run_id = f"msg-{secrets.token_hex(4)}"
        c.codex.emit(run_id, "run.start", "Trinity", prompt=text, mode="message", target=agent, sender=sender)
        job = c.run_agent(run_id, role, f"Message from {sender} to {agent}: {text}")
        if not wait:
            t = asyncio.create_task(job)
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
            return {"node": self.node_id, "path": [self.node_id], "agent": agent, "delivered": "respawned", "run_id": run_id}
        answer = await job
        c.codex.emit(run_id, "run.done", "Trinity")
        return {"node": self.node_id, "path": [self.node_id], "agent": agent, "delivered": "respawned",
                "run_id": run_id, "answer": answer}

    async def handle_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        hops = int(payload.get("hops", 1))
        if hops > self.max_hops:
            raise PermissionError("max fleet hops exceeded")
        return await self.send_message([str(x) for x in payload.get("route") or []], str(payload["agent"]),
                                       str(payload["message"]), str(payload.get("from", "?")), hops,
                                       bool(payload.get("wait")))

    async def run_job(self, kind: str, p: dict[str, Any]) -> dict[str, Any]:
        """Queue handler: retried by the JobQueue if the target node is down."""
        route = [x for x in str(p.get("node", "local")).split("/") if x and x != "local"]
        if kind == "message":
            return await self.send_message(route, p["agent"], p["message"], p.get("from", self.node_id), 0, True)
        if not route and p.get("node") in ("local", "", None):
            res = await (self.core.chat(p["task"]) if p.get("mode") == "chat" else self.core.squad(p["task"], p.get("n")))
            return {"node": self.node_id, "answer": res.get("answer", "")}
        return await self.dispatch(route, p.get("mode", "squad"), p["task"], 0, p.get("n"))

    async def handle_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Server side of /api/fleet/run: forward if routed further, else run locally."""
        route = [str(x) for x in payload.get("route") or []]
        hops = int(payload.get("hops", 1))
        mode, prompt = str(payload.get("mode", "squad")), str(payload.get("prompt", ""))
        if hops > self.max_hops:
            raise PermissionError("max fleet hops exceeded")
        if route:
            res = await self.dispatch(route, mode, prompt, hops, payload.get("n"), payload.get("origin_run", ""))
            return {**res, "path": [self.node_id] + res.get("path", [])}
        c = self.core
        self.core.codex.emit("fleet", "fleet.run", self.node_id, mode=mode, frm=payload.get("from"), hops=hops)
        prev = getattr(c, "fleet_hops", 0)
        c.fleet_hops = hops  # remote agents see how deep they are
        try:
            if mode == "chat":
                res = await c.chat(prompt)
            elif mode == "deep":
                res = await c.deep(prompt, None, payload.get("n"))
            else:
                res = await c.squad(prompt, payload.get("n"))
        finally:
            c.fleet_hops = prev
        answer = res.get("answer") or res.get("report") or ""
        return {"node": self.node_id, "path": [self.node_id], "run_id": res.get("run_id"), "answer": answer}


# ---------------------------------------------------------------- agent tools
def _fleet(ctx: "AgentContext") -> FleetManager:
    f = getattr(ctx.core, "fleet", None)
    if f is None or not f.enabled:
        raise RuntimeError("fleet disabled: set AVRONE_FLEET_TOKEN on every node")
    return f


async def t_fleet_nodes(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    rows = [f"this node: {f.node_id} (parent: {f.parent or 'none'})"]
    for n in f.list_nodes():
        rows.append(f"- {n['node_id']} [{n['status']}] load={n.get('load', 0)} children={n.get('children', [])} "
                    f"runner={n.get('capabilities', {}).get('runner', '?')}")
    return "\n".join(rows) if len(rows) > 1 else rows[0] + "\nno child nodes registered"


async def t_remote_spawn(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    target = str(args.get("node", "auto"))
    route = [p for p in target.split("/") if p] if target != "auto" else []
    hops = getattr(ctx.core, "fleet_hops", 0)
    res = await f.dispatch(route, str(args.get("mode", "squad")), str(args["task"]), hops, args.get("n"), ctx.run_id)
    return f"[{' -> '.join([f.node_id] + res.get('path', []))}] run {res.get('run_id')}\n{res.get('answer', '')}"


async def t_remote_broadcast(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    targets = args.get("nodes") or [n["node_id"] for n in f.list_nodes() if n["status"] == "online"]
    targets = list(dict.fromkeys(str(t) for t in targets))[:8]
    if not targets:
        return "ERROR: no online nodes"
    hops = getattr(ctx.core, "fleet_hops", 0)

    async def one(t: str) -> str:
        try:
            r = await f.dispatch([p for p in t.split("/") if p], str(args.get("mode", "squad")), str(args["task"]),
                                 hops, args.get("n"), ctx.run_id)
            return f"## {t}\n{r.get('answer', '')}"
        except Exception as e:  # noqa: BLE001
            return f"## {t}\nERROR: {type(e).__name__}: {e}"
    return "\n\n".join(await asyncio.gather(*(one(t) for t in targets)))


async def t_fleet_agents(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .presence import directory, render
    f = _fleet(ctx)
    rows = await directory(ctx.core)
    q = str(args.get("filter", "")).lower()
    if q:
        rows = [{**r, "agents": [a for a in r["agents"] if q in json.dumps(a).lower()]} for r in rows
                if q in json.dumps(r).lower()]
    return f"directory from {f.node_id}:\n" + render(rows, bool(args.get("live_only")))


async def t_agent_message(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .presence import directory, find_agent
    f = _fleet(ctx)
    route, name = find_agent(await directory(ctx.core), str(args["agent"]))
    res = await f.send_message([p for p in route.split("/") if p], name, str(args["message"]),
                               f"{f.node_id}/{ctx.name}", getattr(ctx.core, "fleet_hops", 0), bool(args.get("wait")))
    head = f"[{' -> '.join(res.get('path', []))}] {name}: {res.get('delivered')}"
    return head + (f"\n{res['answer']}" if res.get("answer") else f" ({res.get('note') or res.get('run_id')})")


async def t_queue_submit(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    if f.queue is None:
        return "ERROR: queue not running"
    payload = {"task": str(args["task"]), "node": str(args.get("node", "auto")), "mode": str(args.get("mode", "squad")),
               "n": args.get("n"), "from": f"{f.node_id}/{ctx.name}"}
    jid = await f.queue.enqueue("run", payload, int(args.get("priority", 0)), int(args.get("max_attempts", 3)))
    return f"queued job {jid} -> {payload['node']} (retries on failure; check with queue_status)"


async def t_queue_status(ctx: "AgentContext", args: dict[str, Any]) -> str:
    f = _fleet(ctx)
    if f.queue is None:
        return "ERROR: queue not running"
    if args.get("job_id"):
        j = await f.queue.get(str(args["job_id"]))
        if not j:
            return "ERROR: no such job"
        res = j.get("result") or {}
        return (f"job {j['id']} {j['status']} attempts={j['attempts']}/{j['max_attempts']}"
                + (f"\nerror: {j['error']}" if j.get("error") and j["status"] != "done" else "")
                + (f"\n{res.get('answer', '')}" if isinstance(res, dict) else ""))
    return json.dumps(await f.queue.stats())


FLEET_TOOLS = [
    ("fleet_agents", "directory of every agent across the fleet: its node route, device, container, networks, services and current task.",
     '{"filter"?: str, "live_only"?: bool}', t_fleet_agents),
    ("agent_message", "send instructions to a specific agent anywhere in the fleet (\"Builder-3\" or \"router/edge/Builder-3\"); a live agent acts on it inside its own environment, a finished one is respawned there.",
     '{"agent": str, "message": str, "wait"?: bool}', t_agent_message),
    ("queue_submit", "durably queue a squad/chat run for a node (route, \"auto\" or \"local\"); retried if the node is down.",
     '{"task": str, "node"?: str, "mode"?: str, "n"?: int, "priority"?: int, "max_attempts"?: int}', t_queue_submit),
    ("queue_status", "status/result of a queued job, or queue counts if no job_id.", '{"job_id"?: str}', t_queue_status),
    ("fleet_nodes", "list linked AVRONE nodes/routers with status, load and their children.", "{}", t_fleet_nodes),
    ("remote_spawn", "spawn a squad/chat/deep run on a remote AVRONE node; node may be a route like \"router1/edge7\" or \"auto\" (least loaded). Returns its answer.",
     '{"task": str, "node"?: str, "mode"?: "squad"|"chat"|"deep", "n"?: int}', t_remote_spawn),
    ("remote_broadcast", "run the same task on several remote nodes in parallel (default: all online, max 8) and return every answer.",
     '{"task": str, "nodes"?: [str], "mode"?: str, "n"?: int}', t_remote_broadcast),
]


def register_fleet_tools(registry) -> None:
    for name, desc, args_doc, fn in FLEET_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
