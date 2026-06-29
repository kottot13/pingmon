"""Rolling per-target statistics: latency, jitter, loss, trend."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# Latency status thresholds (ms)
EXCELLENT = 40.0
GOOD = 90.0
FAIR = 180.0
POOR = 350.0


@dataclass
class TargetStats:
    """Ring buffer of one target's samples plus derived metrics."""

    maxlen: int = 90
    samples: deque[float | None] = field(default_factory=deque)
    sent: int = 0
    lost: int = 0
    last_ok: float | None = None
    consecutive_fail: int = 0

    def __post_init__(self) -> None:
        self.samples = deque(maxlen=self.maxlen)

    def record(self, latency: float | None) -> None:
        self.sent += 1
        self.samples.append(latency)
        if latency is None:
            self.lost += 1
            self.consecutive_fail += 1
        else:
            self.last_ok = latency
            self.consecutive_fail = 0

    def reset(self) -> None:
        self.samples.clear()
        self.sent = 0
        self.lost = 0
        self.last_ok = None
        self.consecutive_fail = 0

    # --- derived values ---

    @property
    def ok_values(self) -> list[float]:
        return [s for s in self.samples if s is not None]

    @property
    def last(self) -> float | None:
        return self.samples[-1] if self.samples else None

    @property
    def avg(self) -> float | None:
        v = self.ok_values
        return sum(v) / len(v) if v else None

    @property
    def vmin(self) -> float | None:
        v = self.ok_values
        return min(v) if v else None

    @property
    def vmax(self) -> float | None:
        v = self.ok_values
        return max(v) if v else None

    def percentile(self, p: float) -> float | None:
        """Linear-interpolated percentile of successful samples (p in 0–100)."""
        v = sorted(self.ok_values)
        if not v:
            return None
        if len(v) == 1:
            return v[0]
        k = (len(v) - 1) * p / 100.0
        f = int(k)
        c = min(f + 1, len(v) - 1)
        if f == c:
            return v[f]
        return v[f] + (v[c] - v[f]) * (k - f)

    @property
    def median(self) -> float | None:
        return self.percentile(50.0)

    @property
    def jitter(self) -> float | None:
        """Mean absolute difference between consecutive successful samples."""
        v = self.ok_values
        if len(v) < 2:
            return None
        diffs = [abs(v[i] - v[i - 1]) for i in range(1, len(v))]
        return sum(diffs) / len(diffs)

    @property
    def loss(self) -> float:
        return 100.0 * self.lost / self.sent if self.sent else 0.0

    @property
    def r_factor(self) -> float | None:
        """ITU-T E-model R-factor (0–100) from avg latency, jitter and loss.

        Uses effective latency = latency + 2·jitter (jitter hurts ~2× as much
        as latency) and a linear packet-loss impairment.
        """
        lat = self.avg
        if lat is None:
            return None
        jit = self.jitter or 0.0
        eff = lat + 2.0 * jit + 10.0  # +10ms codec/processing allowance
        if eff < 160.0:
            r = 93.2 - eff / 40.0
        else:
            r = 93.2 - (eff - 120.0) / 10.0
        r -= 2.5 * self.loss  # packet-loss impairment
        return max(0.0, min(100.0, r))

    @property
    def mos(self) -> float | None:
        """Pseudo Mean Opinion Score (1.0–4.5) derived from the R-factor."""
        r = self.r_factor
        if r is None:
            return None
        mos = 1.0 + 0.035 * r + 7e-6 * r * (r - 60.0) * (100.0 - r)
        return max(1.0, min(4.5, mos))

    @property
    def status(self) -> str:
        """PENDING | DOWN | UNSTABLE | EXCELLENT | GOOD | FAIR | POOR."""
        if not self.samples:
            return "PENDING"
        if self.consecutive_fail >= 3:
            return "DOWN"
        last = self.last
        if last is None:
            return "UNSTABLE"
        if self.loss >= 20.0:
            return "UNSTABLE"
        if last < EXCELLENT:
            return "EXCELLENT"
        if last < GOOD:
            return "GOOD"
        if last < FAIR:
            return "FAIR"
        if last < POOR:
            return "POOR"
        return "POOR"
