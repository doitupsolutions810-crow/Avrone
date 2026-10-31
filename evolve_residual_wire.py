
# EVOLVE RESIDUAL WIRE — attaches Great Scale evidence to every evolve attempt row
# Source: avrone/evolve.py _attempt_row + greatscale.evidence_snapshot
# Success criterion: no attempt row may authorize action without a residual field.

def _attach_residual(row, failures, runs):
    """Attach a Great Scale evidence snapshot to an evolve attempt row.
    Unmeasured outcomes (runs == 0) get HOLD_UNMEASURED, never a pass."""
    if runs <= 0:
        row["residual"] = {"S": None, "orientation": "unmeasured",
                           "cone": False, "status": "HOLD_UNMEASURED"}
        return row
    S = failures / runs                      # heat = Bernoulli variance
    phi = (5 ** 0.5 - 1) / 2                 # phi^-1
    cone = phi ** -2                         # cone floor
    if failures == 0:
        orient = "pass"
    elif failures == runs:
        orient = "fail"
    else:
        orient = "mixed"
    row["residual"] = {"S": S, "orientation": orient,
                       "cone_floor": cone, "above_cone": S > cone,
                       "status": "READY" if S > cone or orient != "pass"
                       else "READY_CONE"}
    # trend axis: compare to prior attempt if present
    if "prior_S" in row and row["prior_S"] is not None:
        row["residual"]["delta_S"] = S - row["prior_S"]
        row["residual"]["saturated"] = abs(S - row["prior_S"]) < 1e-9
    return row


def _stop_on_saturation(history):
    """Second stop axis: halt when residual trend saturates
    (further iterations re-measure the same heat)."""
    if len(history) < 3:
        return False
    recent = [h.get("residual", {}).get("S") for h in history[-3:]]
    if any(s is None for s in recent):
        return False
    return abs(recent[0] - recent[1]) < 1e-12 and abs(recent[1] - recent[2]) < 1e-12
