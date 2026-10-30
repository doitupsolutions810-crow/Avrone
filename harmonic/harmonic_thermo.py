CAP = 10.0
ES_FLOOR, ES_CEIL = 0.5, 1.0
KG_FLOOR = 0.5
STEEPNESS = 1.0  # bifurcation fix: steep sigmoid folds map into period-2; 1.0 keeps fixed point

class LaneState:
    def __init__(self, committed=0.0, stale_ms=0.0, uncommitted_bytes=0.0, rejected=0):
        self.committed = committed
        self.stale_ms = stale_ms
        self.uncommitted_bytes = uncommitted_bytes
        self.rejected = rejected

    def entropy(self, c_fail=0.5):
        return 0.01 * self.stale_ms + 1.0 * self.uncommitted_bytes + c_fail * self.rejected

    def energy(self, c_fail=0.5):
        e = self.entropy(c_fail)
        t = self.committed + e
        return 1.0 if t < 1e-12 else self.committed / t


class ThermoLedger:
    def __init__(self, window=20):
        self.lanes = {}
        self.history = []
        self.evolve_results = []

    def register(self, name):
        self.lanes[name] = LaneState()

    def record_evolve(self, success):
        self.evolve_results.append(success)

    def evolve_yield(self):
        r = self.evolve_results
        return sum(r) / len(r) if r else 0.85

    def kg(self):
        # evolve-yield governor: high yield -> lower generation slope
        y = self.evolve_yield()
        return max(KG_FLOOR, 1.5 - 1.0 * max(0.0, min(1.0, (y - 0.5) / 0.5)))

    def worst_debt(self, cap=CAP):
        if not self.lanes:
            return 0.0
        return max(l.entropy() / cap for l in self.lanes.values())

    def record(self):
        self.history.append(self.worst_debt())

    def mean_debt(self):
        return sum(self.history) / len(self.history) if self.history else 0.0

    def adaptive_threshold(self, gen0, drain, kd):
        # threshold = equilibrium + noise (distribution-aware), clamped
        denom = self.kg() * gen0 + 2 * kd
        d_star = (gen0 - drain) / denom if denom > 0 else 0.0
        d_star = max(0.0, d_star)
        noise = 0.0
        if len(self.history) > 5:
            m = self.mean_debt()
            var = sum((h - m) ** 2 for h in self.history) / len(self.history)
            noise = var ** 0.5
        return min(0.9, d_star + noise)

    def sigmoid_throttle(self, debt, threshold=0.5):
        # steepness 1.0: gentle response, fixed-point stable
        import math
        x = STEEPNESS * (debt - threshold)
        return 1.0 / (1.0 + math.exp(max(-60, min(60, x))))

    def tone_for_debt(self, debt):
        if debt > 0.7:
            return "literal"
        if debt > 0.3:
            return "abstract"
        return "grounded"

    def regime(self):
        # processor / evolve / idle fractions from ledger shape
        if not self.lanes:
            return {"processor": 0.0, "evolve": 0.0, "idle": 1.0}
        ents = [l.entropy() for l in self.lanes.values()]
        s = sum(ents) or 1.0
        proc = sum(l.committed for l in self.lanes.values()) / s
        ev = sum(l.rejected for l in self.lanes.values()) / s
        idle = max(0.0, 1.0 - proc - ev)
        return {"processor": proc, "evolve": ev, "idle": idle}
