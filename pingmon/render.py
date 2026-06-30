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


def country_label(code: str, country: str, *, accent: str = "#7aa2f7", name_style: str = "bold") -> Text:
    """Render a country cell as ``CODE  Name``.

    The two-letter ISO code stands in for the flag emoji, which most terminals
    (iTerm2 among them) can't display — they fall back to two boxed letters.
    """
    t = Text()
    t.append(f"{code:>2}", style=f"bold {accent}")
    t.append("  ")
    t.append(country, style=name_style)
    return t


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


def quality_block(last: float | None, width: int = 22) -> Text:
    """Latency quality gauge with a word rating, the live value and a scale.

    Two lines: a coloured bar (full = fast) tagged with EXCELLENT/GOOD/… and the
    current ms, then a ``fast ◂───▸ slow`` legend so the bar reads unambiguously.
    """
    out = Text("quality  ", style="#7a7a8c")
    out.append_text(quality_bar(last, width=width))
    out.append("  ")
    if last is None:
        out.append("no signal", style="bold #ff3860")
    else:
        if last < EXCELLENT:
            word = "EXCELLENT"
        elif last < GOOD:
            word = "GOOD"
        elif last < FAIR:
            word = "FAIR"
        elif last < POOR:
            word = "POOR"
        else:
            word = "SLOW"
        out.append(word, style=f"bold {latency_color(last)}")
        out.append(f"  ·  {last:.0f} ms", style="#9a9ab0")
    out.append("\n")
    out.append(" " * 9, style="#6a6a7c")  # align under the bar (len("quality  "))
    out.append("fast ", style="#6a6a7c")
    out.append("◂" + "─" * (width - 2) + "▸", style="#3a3a4a")
    out.append(" slow", style="#6a6a7c")
    return out


def distribution_block(stats, width: int = 22) -> Text:
    """Labelled histogram of the latency spread with median / p90 markers.

    Line 1: ``{min}ms ▏<histogram>▏ {max}ms`` — bars are how often each latency
    occurred, tinted by that latency. Line 2: ``▲`` markers sitting under the
    columns where the median and p90 fall, so you can see the shape at a glance.
    """
    samples = list(stats.samples)
    ok = [s for s in samples if s is not None]
    vmin, vmax = stats.vmin, stats.vmax
    if len(ok) < 2 or vmin is None or vmax is None or vmax <= vmin:
        return Text("collecting samples…", style="#555566")

    span = vmax - vmin
    bins = [0] * width
    for v in ok:
        idx = int((v - vmin) / span * (width - 1))
        bins[min(width - 1, max(0, idx))] += 1
    peak = max(bins) or 1
    blocks = " ▁▂▃▄▅▆▇█"

    prefix = f"{vmin:.0f}ms "
    out = Text(prefix, style="#7a7a8c")
    out.append("▏", style="#3a3a4a")
    for i, count in enumerate(bins):
        if count == 0:
            out.append("·", style="#2a2a38")
        else:
            level = max(1, int(count / peak * 8))
            center = vmin + (i + 0.5) / width * span
            out.append(blocks[level], style=latency_color(center))
    out.append("▏", style="#3a3a4a")
    out.append(f" {vmax:.0f}ms", style="#7a7a8c")

    # markers aligned to the histogram columns
    out.append("\n")
    out.append(" " * (len(prefix) + 1))  # +1 for the leading ▏
    cells: list[tuple[str, str] | None] = [None] * width

    def place(val: float | None, style: str) -> None:
        if val is None:
            return
        col = int(round((val - vmin) / span * (width - 1)))
        cells[min(width - 1, max(0, col))] = ("▲", style)

    place(stats.median, "bold #56c2d6")
    place(stats.percentile(90.0), "bold #c9a0dc")
    for cell in cells:
        if cell is None:
            out.append(" ")
        else:
            out.append(cell[0], style=cell[1])
    return out


def distribution_caption(stats) -> Text:
    """Legend for :func:`distribution_block`: what the markers and bars mean."""
    out = Text()
    out.append("▲", style="bold #56c2d6")
    out.append(" median ", style="#7a7a8c")
    out.append(f"{fmt_ms(stats.median)} ms", style="#56c2d6")
    out.append("    ")
    out.append("▲", style="bold #c9a0dc")
    out.append(" p90 ", style="#7a7a8c")
    out.append(f"{fmt_ms(stats.percentile(90.0))} ms", style="#c9a0dc")
    out.append("\n")
    out.append("taller bar = that latency happened more often", style="#6a6a7c")
    return out


def latency_chart(samples, width: int = 46, height: int = 6) -> Text:
    """Multi-row latency bar chart with absolute colour thresholds.

    Bar height encodes latency relative to the window's peak (so the shape is
    always readable), while each bar's colour is the absolute latency grade
    (green < 40ms, lime < 90, yellow < 180, orange < 350, red above). Dropped
    packets render as a red ``╳`` at the baseline.
    """
    data = list(samples)[-width:]
    ok = [s for s in data if s is not None]
    if not ok:
        blank = " " * max(1, len(data))
        return Text("\n".join(blank for _ in range(height)), style="#2a2a38")

    scale = max(ok) or 1.0
    blocks = " ▁▂▃▄▅▆▇█"
    columns: list[tuple[list[str], str]] = []
    for s in data:
        if s is None:
            chars = [" "] * height
            chars[-1] = "╳"
            columns.append((chars, "bold #ff3860"))
            continue
        total = min(1.0, s / scale) * height
        cells = []
        for r in range(height):  # r = 0 is the bottom row
            level = total - r
            if level >= 1:
                cells.append("█")
            elif level <= 0:
                cells.append(" ")
            else:
                cells.append(blocks[max(1, int(level * 8))])
        columns.append((list(reversed(cells)), latency_color(s)))

    out = Text()
    for r in range(height):
        for chars, style in columns:
            ch = chars[r]
            if ch == " ":
                out.append(" ")
            else:
                out.append(ch, style=style)
        if r != height - 1:
            out.append("\n")
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
