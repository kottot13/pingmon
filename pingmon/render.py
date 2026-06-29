"""Visual helpers: latency colours, status meta, text sparklines."""

from __future__ import annotations

from rich.text import Text

from .stats import EXCELLENT, FAIR, GOOD, POOR, TargetStats

BARS = "▁▂▃▄▅▆▇█"

# status -> (label, colour, marker)
STATUS_META = {
    "EXCELLENT": ("EXCELLENT", "#3ddc84", "●"),
    "GOOD": ("GOOD", "#a6e22e", "●"),
    "FAIR": ("FAIR", "#f4bf4f", "●"),
    "POOR": ("POOR", "#fd8d3c", "▲"),
    "UNSTABLE": ("UNSTABLE", "#ff6f91", "◆"),
    "DOWN": ("DOWN", "#ff3860", "✕"),
    "PENDING": ("PENDING", "#7a7a8c", "◌"),
}


def latency_color(ms: float | None) -> str:
    if ms is None:
        return "#ff3860"
    if ms < EXCELLENT:
        return "#3ddc84"
    if ms < GOOD:
        return "#a6e22e"
    if ms < FAIR:
        return "#f4bf4f"
    if ms < POOR:
        return "#fd8d3c"
    return "#ff3860"


def fmt_ms(value: float | None, width: int = 0) -> str:
    s = "—" if value is None else f"{value:.0f}"
    return s.rjust(width) if width else s


def status_text(stats: TargetStats) -> Text:
    label, color, glyph = STATUS_META[stats.status]
    return Text(f"{glyph} {label}", style=f"bold {color}")


def latency_text(value: float | None, suffix: str = "") -> Text:
    color = latency_color(value)
    s = "—" if value is None else f"{value:.0f}{suffix}"
    return Text(s, style=color)


def sparkline(samples, width: int = 18, lo: float | None = None, hi: float | None = None) -> Text:
    """Coloured text sparkline from the last `width` samples.

    Failures (None) render as a red cross; every bar is tinted with the
    colour of its own latency, giving a lively mini graph.
    """
    data = list(samples)[-width:]
    if not data:
        return Text("·" * width, style="#3a3a4a")

    ok = [s for s in data if s is not None]
    lo = min(ok) if (lo is None and ok) else (lo if lo is not None else 0.0)
    hi = max(ok) if (hi is None and ok) else (hi if hi is not None else 1.0)
    span = (hi - lo) or 1.0

    out = Text()
    for s in data:
        if s is None:
            out.append("╳", style="bold #ff3860")
            continue
        idx = int((s - lo) / span * (len(BARS) - 1))
        idx = max(0, min(len(BARS) - 1, idx))
        out.append(BARS[idx], style=latency_color(s))
    pad = width - len(data)
    if pad > 0:
        return Text("·" * pad, style="#3a3a4a") + out
    return out


def distribution_strip(samples, width: int = 24) -> Text:
    """SmokePing-style density strip across the latency range of the window.

    The x-axis runs from the window's min to max latency; each column is a bin
    shaded by how many samples fall in it (darker = denser), tinted by the
    latency at that point. Reveals whether a link is tight or smeared.
    """
    ok = [s for s in samples if s is not None]
    if len(ok) < 2:
        return Text("·" * width, style="#3a3a4a")
    lo, hi = min(ok), max(ok)
    span = (hi - lo) or 1.0
    bins = [0] * width
    for v in ok:
        idx = int((v - lo) / span * (width - 1))
        bins[min(width - 1, max(0, idx))] += 1
    peak = max(bins) or 1
    shades = " ░▒▓█"
    out = Text()
    for i, count in enumerate(bins):
        if count == 0:
            out.append("·", style="#2a2a38")
            continue
        level = max(1, int(count / peak * (len(shades) - 1)))
        center = lo + (i + 0.5) / width * span
        out.append(shades[level], style=latency_color(center))
    return out


def loss_text(loss: float) -> Text:
    if loss <= 0:
        return Text("0%", style="#3ddc84")
    if loss < 5:
        return Text(f"{loss:.0f}%", style="#f4bf4f")
    return Text(f"{loss:.0f}%", style="bold #ff3860")


def quality_bar(value: float | None, width: int = 20) -> Text:
    """Horizontal quality gauge: lower latency -> longer bar."""
    if value is None:
        return Text("░" * width, style="#ff3860")
    # 0ms -> full, 400ms+ -> empty
    frac = max(0.0, min(1.0, 1.0 - value / 400.0))
    filled = int(round(frac * width))
    color = latency_color(value)
    bar = Text("█" * filled, style=color)
    bar.append("░" * (width - filled), style="#2a2a38")
    return bar
