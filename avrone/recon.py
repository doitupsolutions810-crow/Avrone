"""Advanced reconnaissance (v0.10): deeper, still bounded, authorized-only recon.

Scope and honesty:
  * These tools work on hosts/domains you name, one target per call. No sweeps, no
    ranges, no crawling. port_scan refuses to run without authorized=true and is
    capped to a small port list, because scanning hosts you do not own is abuse.
  * Everything uses public data or a direct connection to the named target: TLS
    certificates, HTTP response headers, public DNS, and certificate-transparency
    logs (crt.sh). Nothing here touches private infrastructure, satellites, towers,
    or any capability AVRONE does not have.
  * Every call is logged to the Codex action log.

Tools: tls_inspect, http_fingerprint, port_scan, cert_transparency, dns_enum.
Env: AVRONE_RECON=allow|deny (default allow), AVRONE_RECON_ALLOWLIST (comma hosts/suffixes; empty = any).
"""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

# A deliberately small, common-service port set. Not a full scanner.
COMMON_PORTS = [21, 22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995, 3306, 3389, 5432, 6379, 8080, 8443]
SERVICE = {21: "ftp", 22: "ssh", 25: "smtp", 53: "dns", 80: "http", 110: "pop3", 143: "imap",
           443: "https", 465: "smtps", 587: "submission", 993: "imaps", 995: "pop3s",
           3306: "mysql", 3389: "rdp", 5432: "postgres", 6379: "redis", 8080: "http-alt", 8443: "https-alt"}


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


def _denied() -> str | None:
    if os.getenv("AVRONE_RECON", "allow").lower() == "deny":
        return "ERROR: reconnaissance is disabled on this node (AVRONE_RECON=deny)"
    return None


def _allowed_host(host: str) -> bool:
    allow = [h.strip().lower() for h in os.getenv("AVRONE_RECON_ALLOWLIST", "").split(",") if h.strip()]
    if not allow:
        return True
    h = host.lower()
    # exact host or a true subdomain only: "evilmysite.com" must NOT match "mysite.com"
    return any(h == a.lstrip(".") or h.endswith("." + a.lstrip(".")) for a in allow)


def _host_arg(args: dict[str, Any]) -> tuple[str, str | None]:
    host = str(args.get("host") or args.get("domain") or args.get("target") or "").strip()
    host = host.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    if not host:
        return "", 'ERROR: give {"host": "example.com"}'
    if not _allowed_host(host):
        return host, f"ERROR: {host} is not in AVRONE_RECON_ALLOWLIST"
    return host, None


async def t_tls_inspect(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if (d := _denied()):
        return d
    host, err = _host_arg(args)
    if err:
        return err
    port = int(args.get("port", 443))

    def _get():
        ctx_ssl = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx_ssl.wrap_socket(sock, server_hostname=host) as ss:
                return ss.getpeercert(), ss.version(), ss.cipher()

    try:
        cert, ver, cipher = await asyncio.to_thread(_get)
    except (OSError, ssl.SSLError) as e:
        return f"ERROR: TLS to {host}:{port}: {type(e).__name__}: {e}"
    _emit(ctx, "recon.tls", host=host, port=port)
    subj = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    sans = [v for t, v in cert.get("subjectAltName", []) if t == "DNS"]
    not_after = cert.get("notAfter", "")
    days = ""
    try:
        exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = f" ({(exp - datetime.now(timezone.utc)).days} days left)"
    except ValueError:
        pass
    return (f"{host}:{port}  {ver}  {cipher[0] if cipher else ''}\n"
            f"subject CN: {subj.get('commonName', '?')}\n"
            f"issuer: {issuer.get('organizationName', issuer.get('commonName', '?'))}\n"
            f"valid: {cert.get('notBefore', '?')} -> {not_after}{days}\n"
            f"SANs ({len(sans)}): {', '.join(sans[:40])}")


async def t_http_fingerprint(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if (d := _denied()):
        return d
    host, err = _host_arg(args)
    if err:
        return err
    import httpx
    scheme = str(args.get("scheme", "https"))
    url = f"{scheme}://{host}/"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url, headers={"user-agent": "AVRONE-recon/0.10"})
    except httpx.HTTPError as e:
        return f"ERROR: HTTP to {url}: {type(e).__name__}: {e}"
    _emit(ctx, "recon.http", host=host, status=r.status_code)
    interesting = ["server", "x-powered-by", "via", "x-cache", "cf-ray", "x-amz-cf-id", "content-type",
                   "strict-transport-security", "content-security-policy", "x-frame-options",
                   "set-cookie", "x-served-by", "x-vercel-id", "x-github-request-id"]
    sec = {"strict-transport-security", "content-security-policy", "x-frame-options",
           "x-content-type-options", "referrer-policy"}
    lines = [f"{url} -> {r.status_code}  (final: {r.url})"]
    for h in interesting:
        if h in r.headers:
            v = r.headers[h]
            lines.append(f"  {h}: {v[:160]}")
    missing = sorted(sec - {k.lower() for k in r.headers})
    lines.append(f"missing security headers: {', '.join(missing) if missing else 'none'}")
    return "\n".join(lines)


async def t_port_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if (d := _denied()):
        return d
    if not bool(args.get("authorized")):
        return ('ERROR: port_scan needs {"authorized": true} to confirm you own or are authorized to scan '
                "this host. Scanning third-party hosts is abuse and is refused.")
    host, err = _host_arg(args)
    if err:
        return err
    ports = args.get("ports") or COMMON_PORTS
    if not isinstance(ports, list) or len(ports) > 32:
        return "ERROR: ports must be a list of at most 32 integers"
    try:
        ports = sorted({int(p) for p in ports})
    except (TypeError, ValueError):
        return "ERROR: ports must be integers"
    if any(p < 1 or p > 65535 for p in ports):
        return "ERROR: ports must be between 1 and 65535"

    async def probe(p: int) -> tuple[int, bool]:
        try:
            fut = asyncio.open_connection(host, p)
            r, w = await asyncio.wait_for(fut, timeout=2.0)
            w.close()
            try:
                await w.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return p, True
        except (OSError, asyncio.TimeoutError):
            return p, False

    results = await asyncio.gather(*(probe(p) for p in ports))
    _emit(ctx, "recon.portscan", host=host, ports=len(ports))
    openp = [f"{p}/{SERVICE.get(p, '?')}" for p, ok in sorted(results) if ok]
    return (f"port_scan {host}  ({len(ports)} ports)\n"
            f"open: {', '.join(openp) if openp else 'none'}\n"
            f"closed/filtered: {len(ports) - len(openp)}")


async def t_cert_transparency(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if (d := _denied()):
        return d
    host, err = _host_arg(args)
    if err:
        return err
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://crt.sh/", params={"q": f"%.{host}", "output": "json"},
                            headers={"user-agent": "AVRONE-recon/0.10"})
            r.raise_for_status()
            rows = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return f"ERROR: crt.sh lookup for {host}: {type(e).__name__}: {e}"
    names: set[str] = set()
    for row in rows:
        for n in str(row.get("name_value", "")).splitlines():
            n = n.strip().lstrip("*.").lower()
            if n.endswith(host):
                names.add(n)
    _emit(ctx, "recon.ct", host=host, found=len(names))
    sub = sorted(names)
    head = "\n".join(sub[:80])
    more = f"\n... (+{len(sub) - 80} more)" if len(sub) > 80 else ""
    return (f"certificate-transparency subdomains of {host}: {len(sub)} unique (public crt.sh data)\n{head}{more}"
            if sub else f"no certificate-transparency records found for {host}")


async def t_dns_enum(ctx: "AgentContext", args: dict[str, Any]) -> str:
    if (d := _denied()):
        return d
    host, err = _host_arg(args)
    if err:
        return err
    import httpx
    types = args.get("types") or ["A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA"]
    out = [f"DNS for {host}"]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for t in types[:10]:
                r = await c.get("https://dns.google/resolve", params={"name": host, "type": t},
                                headers={"accept": "application/dns-json"})
                data = r.json()
                ans = [a.get("data", "") for a in data.get("Answer", []) if a.get("data")]
                out.append(f"  {t}: {', '.join(ans[:12]) if ans else '-'}")
    except (httpx.HTTPError, ValueError) as e:
        return f"ERROR: DNS lookup for {host}: {type(e).__name__}: {e}"
    _emit(ctx, "recon.dns", host=host)
    return "\n".join(out)


RECON_TOOLS = [
    ("tls_inspect", "inspect one host's TLS certificate: issuer, validity, and all SAN hostnames.",
     '{"host": str, "port"?: int}', t_tls_inspect),
    ("http_fingerprint", "fetch one host's HTTP response headers: server/tech hints and which security headers are missing.",
     '{"host": str, "scheme"?: "https|http"}', t_http_fingerprint),
    ("port_scan", "TCP-connect scan a small port set on ONE host you own/are authorized to scan (needs authorized=true).",
     '{"host": str, "authorized": true, "ports"?: [int]}', t_port_scan),
    ("cert_transparency", "enumerate a domain's subdomains from public certificate-transparency logs (crt.sh).",
     '{"host": str}', t_cert_transparency),
    ("dns_enum", "resolve A/AAAA/MX/NS/TXT/CNAME/SOA records for one host via public DNS-over-HTTPS.",
     '{"host": str, "types"?: [str]}', t_dns_enum),
]


def register_recon_tools(registry) -> None:
    for name, desc, args_doc, fn in RECON_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
