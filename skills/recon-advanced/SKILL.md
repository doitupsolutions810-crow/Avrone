---
name: recon-advanced
description: Authorized network reconnaissance playbook stressing one-target-per-call and allowlist discipline.
---

1. Confirm authorized=true and target is on the explicit allowlist before any probe.
2. One target per call: use tls_inspect, http_fingerprint, cert_transparency, dns_enum, or port_scan (with authorized=true only) on exactly one host at a time.
3. Port_scan: limit to a small authorized port set; never sweep.
4. Sequence: start with dns_enum and cert_transparency for discovery, then tls_inspect, http_fingerprint, and targeted port_scan.
5. Always respect allowlist; abort on any unauthorized target.
6. Report findings per tool with exact outputs, noting discipline and any blocks.
