"""Regenerate the documentation screenshots in `docs/*.svg`.

Run from the repo root:

    python tools/screenshots.py            # writes docs/*.svg
    python tools/screenshots.py /tmp/out   # writes elsewhere

It renders the TUI head­lessly through Textual's test pilot with synthetic,
deterministic latency data, so the result never depends on the network and never
leaks whatever targets happen to be in your personal config. Rasterise to PNG for
a quick look with `qlmanage -t -s 1200 -o . docs/screenshot.svg` on macOS.

Re-run this whenever a change alters anything the screenshots show (the table,
the detail panel, the Region Advisor, or the traceroute view).
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile

# Isolate from the real user config: a fresh temp path => the built-in set only.
_tmp = tempfile.mkdtemp(prefix="pingmon-shot-")
os.environ["PINGMON_CONFIG"] = os.path.join(_tmp, "config.toml")

from pingmon.app import PingMonApp, TracerouteScreen  # noqa: E402
from pingmon.render import latency_text, loss_text  # noqa: E402
from rich.text import Text  # noqa: E402

DOCS = sys.argv[1] if len(sys.argv) > 1 else "docs"
SIZE = (132, 41)

# Per-country baseline latency (ms) for a believable, varied dashboard.
BASELINE = {
    "Netherlands": 18, "Germany": 27, "United Kingdom": 33, "France": 24,
    "Cyprus": 96, "Italy": 41, "Spain": 52, "Greece": 88, "Sweden": 46,
    "Ireland": 38, "United States": 132,
}
WAVE = [math.sin(i / 3.0) for i in range(60)]


def fill(app: PingMonApp) -> None:
    """Stop live workers and inject deterministic samples + geo enrichment."""
    app.workers.cancel_all()
    for idx, mon in enumerate(app.monitors):
        base = BASELINE.get(mon.target.country, 60) + idx % 3 * 4
        mon.stats.reset()
        for i in range(60):
            if mon.target.country == "Cyprus" and i in (41, 42):
                mon.stats.record(None)  # one drop, to show the red marker
                continue
            mon.stats.record(max(3.0, base + WAVE[i] * (4 + base * 0.06)))
        mon.ip = f"203.0.113.{10 + idx}"
        mon.geo = {
            "city": {"Netherlands": "Amsterdam", "Germany": "Frankfurt"}.get(
                mon.target.country, mon.target.country),
            "regionName": mon.target.country,
            "as": "AS60781 LeaseWeb Netherlands B.V.",
            "isp": "LeaseWeb",
            "query": mon.ip,
        }
    app.selected = app.monitors[0]


async def main_shot() -> None:
    app = PingMonApp()
    async with app.run_test(size=SIZE) as pilot:
        fill(app)
        app._resort()
        app._tick()
        await pilot.pause()
        app.save_screenshot(f"{DOCS}/screenshot.svg")
    print("wrote screenshot.svg")


async def advisor_shot() -> None:
    app = PingMonApp()
    async with app.run_test(size=SIZE) as pilot:
        fill(app)
        app._tick()
        await pilot.press("g")  # open the Region Advisor
        await pilot.pause()
        app.save_screenshot(f"{DOCS}/advisor.svg")
    print("wrote advisor.svg")


async def trace_shot() -> None:
    app = PingMonApp()
    async with app.run_test(size=SIZE) as pilot:
        fill(app)
        app._tick()
        target = next(m.target for m in app.monitors if m.target.host == "ftp.fau.de")
        screen = TracerouteScreen(target)
        await app.push_screen(screen)
        await pilot.pause()
        app.workers.cancel_all()  # stop the real traceroute worker
        table = screen.query_one("#trace-table")
        table.clear()
        hops = [
            (1, "192.168.1.1", 0.0, [0.6, 0.7, 0.6], 0.6, "LAN"),
            (2, "100.64.0.1", 0.0, [3.1, 3.4, 3.0], 3.0, "CGNAT"),
            (3, "84.116.130.1", 0.0, [9.2, 9.8, 9.1], 9.1, "AT  Vienna  AS1299"),
            (4, "62.115.120.9", 0.0, [14.0, 15.1, 13.8], 13.8, "DE  Frankfurt  AS1299"),
            (5, "131.188.3.1", 0.0, [27.3, 28.0, 26.9], 26.9, "DE  Erlangen  AS680"),
            (6, "131.188.12.40", 0.0, [27.8, 28.4, 27.5], 27.5, "DE  Erlangen  AS680"),
        ]
        for hop, host, loss, probes, best, loc in hops:
            table.add_row(
                Text(str(hop), style="#9a9ab0"),
                Text(host, style="#c9c9d6"),
                loss_text(loss),
                Text("  ".join(f"{p:.1f}" for p in probes), style="#9a9ab0"),
                latency_text(best),
                Text(loc, style="#9a9ab0"),
                key=f"h{hop}",
            )
        screen.query_one("#trace-foot").update(Text("done   ·   Esc close", style="#6a6a7c"))
        await pilot.pause()
        app.save_screenshot(f"{DOCS}/traceroute.svg")
    print("wrote traceroute.svg")


def run() -> None:
    asyncio.run(main_shot())
    asyncio.run(advisor_shot())
    asyncio.run(trace_shot())


if __name__ == "__main__":
    run()
