"""pack_list + peer_audit.

    pack_list {}
        Every agent pack in data/agentpacks/: name, size, and whether it verifies (manifest, sha256s,
        quota, no links/traversal/decompression bomb) - or the exact reason it would be refused on
        import. Verification runs read_pack in memory; nothing is extracted. Read-only.

    peer_audit {}
        Every registered fleet peer checked against today's url rules (check_node_url): a fleet.json
        from before v0.31 may still hold a metadata/link-local/credentialed url this node would POST
        to (since v0.31 every outgoing call refuses such a url). Also shows each peer's status. Read-only:
        the fix is the peer re-registering with a valid url.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

MAX_LIST = 50


def _data_dir(ctx: "AgentContext") -> Path:
    s = getattr(getattr(ctx, "core", None), "settings", None)
    return Path(getattr(s, "data_dir", "./data"))


def _verify(p: Path) -> tuple[str, str]:
    from . import agentpack
    try:
        man, mem, blobs = agentpack.read_pack(p.read_bytes())
    except agentpack.PackError as e:
        return "REFUSED", str(e)[:160]
    except OSError as e:
        return "UNREADABLE", e.__class__.__name__
    return "OK", f"agent {man.get('agent_id')} from {man.get('origin')}: {len(blobs)} file(s), {len(mem)} memory record(s)"


async def t_pack_list(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from . import agentpack
    d = agentpack.pack_dir(_data_dir(ctx))
    if not d.is_dir():
        return "pack_list: no agent packs yet (agent_pack_export creates them)"
    files = sorted((p for p in d.iterdir() if p.name.endswith(".tar.gz") and not p.name.startswith(".")),
                   key=lambda p: p.name)
    if not files:
        return "pack_list: no agent packs yet (agent_pack_export creates them)"
    lines = [f"pack_list: {len(files)} pack(s) in agentpacks/" + (f", first {MAX_LIST} shown" if len(files) > MAX_LIST else "")]
    bad = 0
    for p in files[:MAX_LIST]:
        if p.is_symlink():
            lines.append(f"- {p.name}: REFUSED - symlink (import refuses links)")
            bad += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        verdict, detail = await asyncio.to_thread(_verify, p)
        bad += verdict != "OK"
        lines.append(f"- {p.name} ({size} bytes): {verdict} - {detail}")
    if bad:
        lines.append(f"{bad} pack(s) would be refused on import; delete or re-export them")
    return "\n".join(lines)


async def t_peer_audit(ctx: "AgentContext", args: dict[str, Any]) -> str:
    from .fleet import check_node_url
    f = getattr(getattr(ctx, "core", None), "fleet", None)
    if f is None or not getattr(f, "enabled", False):
        return "peer_audit: fleet disabled on this node (no AVRONE_FLEET_TOKEN)"
    nodes = dict(getattr(f, "nodes", {}) or {})
    if not nodes:
        return "peer_audit: no registered peers"
    lines, bad = [], 0
    for nid in sorted(nodes):
        n = nodes[nid] if isinstance(nodes[nid], dict) else {}
        try:
            status = f.status_of(n)
        except Exception:  # noqa: BLE001
            status = "unknown"
        try:
            check_node_url(n.get("url"))
            verdict = "ok"
        except ValueError as e:
            verdict = f"BAD URL - {e}"
            bad += 1
        lines.append(f"- {nid} [{status}]: {verdict}")
    head = f"peer_audit: {len(nodes)} peer(s), {bad} with a url this node should not call"
    tail = ["calls to these peers are refused; have each re-register with a valid url"] if bad else []
    return "\n".join([head, *lines, *tail])


PACKPEER_TOOLS = [
    Tool("pack_list", "read-only: list agent packs and verify each in memory (or say why import would refuse it).",
         "{}", t_pack_list, source="builtin", schema={}),
    Tool("peer_audit", "read-only: check every registered fleet peer's url against current rules (no metadata/"
         "link-local/credentials) plus its status. Never prints urls.", "{}", t_peer_audit, source="builtin", schema={}),
]


def register_packpeer_tools(registry) -> None:
    for t in PACKPEER_TOOLS:
        registry.register(Tool(t.name, t.description, t.args_doc, t.fn, source=t.source,
                               schema=dict(t.schema) if t.schema is not None else None))
