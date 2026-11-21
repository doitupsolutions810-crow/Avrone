# AVRONE — read-only mirror (W0 freeze prep)

**W0 canonicalization executed 2026-11-21.** AVRONE development now continues in two homes:

- **Kernel:** [`crow-os`](https://github.com/doitupsolutions810-crow/crow-os) — the Crow OS kernel. The 38-file `crow-os-workspace` application moved to [`apps/crow_os_workspace`](https://github.com/doitupsolutions810-crow/crow-os/tree/main/apps/crow_os_workspace) (PR #4, merged `1a016d7`).
- **Canonical runtime:** [`avrone-daemon`](https://github.com/doitupsolutions810-crow/avrone-daemon) — designated sole active AVRONE writer (W0 decision: larger runtime, 96 evidence files, 207 test files, v0.161.0).

This repository was frozen at commit `52cc1fe` before any change. The complete original
README and full tree are preserved on the branch
[`mirror/pre-W0-freeze-prep`](https://github.com/doitupsolutions810-crow/Avrone/tree/mirror/pre-W0-freeze-prep).

Do not push here. The owner will flip this repository to read-only / archived in GitHub
Settings to finish the one-writer invariant (W0 exit condition 1).

Receipts for this decision are appended to the
[`attested-history`](https://github.com/doitupsolutions810-crow/attested-history) ledger.

---

