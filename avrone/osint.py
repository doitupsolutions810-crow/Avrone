"""OSINT aggregation (v0.11): data-driven profiles from the bounded recon tools.

Honesty and scope (unchanged from recon.py):
  * Public data only — DNS, certificate-transparency logs, TLS certs, HTTP headers,
    RDAP. One domain/host per call. No sweeps.
  * This profiles DOMAINS and INFRASTRUCTURE you name. It does NOT profile, locate,
    or deanonymize private individuals, and nothing here reaches private
    infrastructure or any capability AVRONE does not actually have.
  * Honours AVRONE_RECON / AVRONE_RECON_ALLOWLIST and logs every call to Codex.

Tools: osint_domain (aggregate a domain's public footprint), osint_metrics (recon/
osint activity rolled up from the Codex action logs).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import recon
from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext


async def t_osint_domain(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """One structured public-footprint profile of a domain: DNS, CT subdomains, TLS, HTTP."""
    if (d := recon._denied()):
        return d
    host, err = recon._host_arg(args)
    if err:
        return err
    report: dict[str, Any] = {"host": host}
    # Each section degrades independently: a failure in one does not sink the profile.
    report["dns"] = await recon.t_dns_enum(ctx, {"host": host, "types": ["A", "AAAA", "MX", "NS", "TXT"]})
    report["tls"] = await recon.t_tls_inspect(ctx, {"host": host})
    report["http"] = await recon.t_http_fingerprint(ctx, {"host": host})
    ct = await recon.t_cert_transparency(ctx, {"host": host})
    report["subdomains"] = ct
    try:
        ctx.core.codex.emit(ctx.run_id, "osint.domain", ctx.name, host=host)
    except Exception:  # noqa: BLE001
        pass
    # Render compact, section by section.
    sub_count = ""
    first = ct.splitlines()[0] if ct else ""
    if "unique" in first:
        sub_count = first
    return (f"OSINT profile — {host} (public data)\n"
            f"\n[DNS]\n{report['dns']}\n"
            f"\n[TLS]\n{report['tls']}\n"
            f"\n[HTTP]\n{report['http']}\n"
            f"\n[Subdomains] {sub_count or ct[:200]}")


async def t_osint_metrics(ctx: "AgentContext", args: dict[str, Any]) -> str:
    """Roll up recon/OSINT activity from the Codex run logs: tool counts and top targets."""
    data_dir = Path(ctx.core.settings.data_dir)
    log_dir = data_dir / "runs"
    kinds = Counter()
    hosts = Counter()
    runs = 0
    if log_dir.exists():
        for p in log_dir.glob("*.jsonl"):
            runs += 1
            for line in p.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                k = ev.get("kind", "")
                if k.startswith(("recon.", "osint.")):
                    kinds[k] += 1
                    h = (ev.get("data") or {}).get("host")
                    if h:
                        hosts[h] += 1
    if not kinds:
        return "no recon/OSINT activity recorded yet"
    lines = [f"recon/OSINT metrics across {runs} run logs", "", "by tool:"]
    lines += [f"  {k}: {n}" for k, n in kinds.most_common()]
    lines += ["", "top targets:"]
    lines += [f"  {h}: {n}" for h, n in hosts.most_common(10)]
    lines += ["", f"total probes: {sum(kinds.values())}"]
    return "\n".join(lines)


OSINT_TOOLS = [
    ("osint_domain", "aggregate one domain's public footprint (DNS, TLS, HTTP, certificate-transparency subdomains) "
     "into a single profile. Public data, one domain per call; no private-individual targeting.",
     '{"host": str}', t_osint_domain),
    ("osint_metrics", "roll up recorded recon/OSINT activity from the action logs: counts per tool and top targets.",
     "{}", t_osint_metrics),
]


def register_osint_tools(registry) -> None:
    for name, desc, args_doc, fn in OSINT_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
