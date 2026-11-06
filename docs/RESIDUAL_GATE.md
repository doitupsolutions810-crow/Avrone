# Residual Gate (Descent CIV)

**Branch:** `feature/residual-gate-civ`  
**Non-destructive:** adds `avrone/residual_gate.py` only. Does **not** overwrite `greatscale.py`, `trinity.py`, or any existing module.

## Peer relationship

| Module | Role |
|--------|------|
| `avrone/greatscale.py` | Shared residual math + evidence channel (S = heat(p), Psi = CONE + S) |
| `avrone/residual_gate.py` | Fail-closed Lead admission policy over the same coordinate |

## Env

```
AVRONE_RESIDUAL_GATE=0   # default OFF — log-compatible; set 1 to veto Lead on !accepted
RESIDUAL_GATE=0          # alias
RESIDUAL_LOG_ONLY=0
RESIDUAL_QUADRATURE_TOLERANCE=0.001
```

## Lead hook (optional, one site)

```python
from avrone.residual_gate import residual_snapshot, admit_lead

snap = residual_snapshot()  # or failures=/runs= from suite
decision = admit_lead(snap)
if not decision["ok"]:
    codex.emit(run_id, "lead.rejected", "Residual", residual=snap)
    return ...
codex.emit(run_id, "residual.snapshot", "Residual", **snap)
# proceed lead.merge
```

## Success criteria

- [x] Same CONE / heat as greatscale (peer, not fork)
- [x] Gate default OFF
- [x] Fail-closed when gate ON and bounds fail
- [ ] Optional trinity Lead call site (separate PR after review)
- [ ] Docker host VERIFY green

## Great Scale map

Layer 0–3: residual math + evidence heat (greatscale)  
Layer 4–5: residual gate policy (this module)  
Layer 6–7: Lead/CodeX emit + operator VERIFY
