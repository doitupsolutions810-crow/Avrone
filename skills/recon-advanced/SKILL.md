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

7. Bound every probe by an explicit target, port set and time budget. A timeout is an unknown result, not
   proof of a negative finding; do not retry indefinitely.
8. For long or remote campaigns, checkpoint plans and accepted observations before migration. Verify the
   destination node, memory namespace, agent-home quota, and pack hash before resuming.
9. Preserve governor and signed-dispatch controls: do not disable thermal, memory, CPU, hop-count, allowlist,
   signature, or audit checks to increase reach. Never request host-root or arbitrary shell dispatch in place
   of a scoped tool.
