"""Region Advisor: composite per-target quality score by use-case profile."""

from __future__ import annotations

from .stats import TargetStats

# profile key -> (label, one-line description)
PROFILES = ["voip", "gaming", "web", "bulk"]
PROFILE_META = {
    "voip": ("VoIP / Video call", "jitter & loss dominate (MOS / E-model)"),
    "gaming": ("Gaming", "raw latency + jitter, loss punished hard"),
    "web": ("Web / API", "latency-led, mild loss penalty"),
    "bulk": ("Bulk / Backup", "loss-led, tolerant of latency"),
}


def score(st: TargetStats, profile: str) -> float | None:
    """Return a 0–100 quality score (higher is better), or None if no data."""
    if st.status in ("DOWN",):
        return 0.0
    lat = st.avg
    if lat is None:
        return None
    jit = st.jitter or 0.0
    loss = st.loss

    if profile == "voip":
        mos = st.mos
        if mos is None:
            return None
        return (mos - 1.0) / 3.5 * 100.0  # map MOS 1..4.5 -> 0..100

    if profile == "gaming":
        penalty = lat * 0.25 + jit * 1.0 + loss * 6.0
    elif profile == "web":
        penalty = lat * 0.18 + jit * 0.3 + loss * 4.0
    elif profile == "bulk":
        penalty = lat * 0.05 + jit * 0.1 + loss * 8.0
    else:
        penalty = lat * 0.2 + jit * 0.5 + loss * 5.0

    return max(0.0, 100.0 - penalty)


def grade(score_value: float | None) -> str:
    if score_value is None:
        return "—"
    if score_value >= 85:
        return "A"
    if score_value >= 70:
        return "B"
    if score_value >= 50:
        return "C"
    if score_value >= 30:
        return "D"
    return "F"


def reason(st: TargetStats, profile: str) -> str:
    """Short human explanation for the score of a target under a profile."""
    if st.status == "DOWN":
        return "unreachable"
    lat = st.avg
    if lat is None:
        return "warming up"
    jit = st.jitter or 0.0
    loss = st.loss
    if profile == "voip" and st.mos is not None:
        return f"MOS {st.mos:.1f} · {lat:.0f}ms / jit {jit:.0f} / loss {loss:.0f}%"
    return f"{lat:.0f}ms · jit {jit:.0f}ms · loss {loss:.0f}%"
