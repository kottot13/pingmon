"""pingmon TUI: live latency & availability monitoring per country."""

from __future__ import annotations

import asyncio
from datetime import datetime

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Sparkline,
    Static,
)

from .config import Config, Target, load_config, save_config
from .netutil import desktop_notify, flag_emoji, geo_lookup, is_global_ip, traceroute
from .pinger import resolve, tcp_ping
from .render import (
    STATUS_META,
    distribution_strip,
    latency_color,
    latency_text,
    loss_text,
    quality_bar,
    sparkline,
)
from .scoring import PROFILE_META, PROFILES, grade, reason, score
from .stats import TargetStats

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Monitor:
    """A target bundled with its stats and background ping worker."""

    def __init__(self, target: Target, history: int) -> None:
        self.target = target
        self.stats = TargetStats(maxlen=history)
        self.ip: str | None = None
        self.geo: dict | None = None  # city / ISP / ASN from GeoIP
        self.worker = None

    @property
    def key(self) -> str:
        return self.target.key


class TargetFormScreen(ModalScreen[Target | None]):
    """Modal dialog to add or edit a target (fields pre-filled when editing)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, initial: Target | None = None, title: str = "New target") -> None:
        super().__init__()
        self.initial = initial
        self.title_text = title

    def compose(self) -> ComposeResult:
        init = self.initial
        ok_label = "Save" if init is not None else "Add"
        with Vertical(id="add-dialog"):
            yield Label(self.title_text, id="add-title")
            yield Input(
                placeholder="Country (e.g. Poland)", id="in-country",
                value=init.country if init else "",
            )
            yield Input(
                placeholder="Flag emoji 🇵🇱 (optional)", id="in-flag",
                value=init.flag if init else "",
            )
            yield Input(
                placeholder="Host or IP (e.g. 1.1.1.1)", id="in-host",
                value=init.host if init else "",
            )
            yield Input(
                placeholder="Port (default 443)", id="in-port",
                value=str(init.port) if init else "443",
            )
            yield Label(
                "Leave country/flag empty to auto-detect (GeoIP).",
                id="add-hint",
            )
            with Horizontal(id="add-buttons"):
                yield Button(ok_label, variant="success", id="add-ok")
                yield Button("Cancel", variant="default", id="add-cancel")

    def on_mount(self) -> None:
        self.query_one("#in-country", Input).focus()

    @on(Button.Pressed, "#add-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#add-ok")
    def _submit(self) -> None:
        host = self.query_one("#in-host", Input).value.strip()
        if not host:
            self.query_one("#in-host", Input).focus()
            return
        # empty country/flag are kept empty so the app can auto-detect via GeoIP
        country = self.query_one("#in-country", Input).value.strip()
        flag = self.query_one("#in-flag", Input).value.strip()
        port_raw = self.query_one("#in-port", Input).value.strip() or "443"
        try:
            port = int(port_raw)
        except ValueError:
            port = 443
        self.dismiss(Target(country=country, flag=flag, host=host, port=port, source="user"))


def _score_bar(value: float | None, width: int = 12) -> Text:
    """0–100 score rendered as a coloured bar."""
    if value is None:
        return Text("·" * width, style="#3a3a4a")
    filled = int(round(value / 100.0 * width))
    if value >= 70:
        color = "#3ddc84"
    elif value >= 50:
        color = "#f4bf4f"
    elif value >= 30:
        color = "#fd8d3c"
    else:
        color = "#ff3860"
    bar = Text("█" * filled, style=color)
    bar.append("░" * (width - filled), style="#2a2a38")
    return bar


class AdvisorScreen(ModalScreen):
    """Region Advisor: ranks regions by a composite score for the chosen profile."""

    BINDINGS = [
        Binding("escape,g", "close", "Close"),
        Binding("left,h,[", "prev_profile", "Prev profile"),
        Binding("right,l,],p,tab", "next_profile", "Next profile"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="advisor-dialog"):
            yield Static(id="advisor-head")
            yield DataTable(id="advisor-table", cursor_type="none", zebra_stripes=True)
            yield Static(id="advisor-foot")

    def on_mount(self) -> None:
        t = self.query_one("#advisor-table", DataTable)
        t.add_column("#", width=2)
        t.add_column("Region", width=20)
        t.add_column("Best endpoint", width=30)
        t.add_column("Score", width=16)
        t.add_column("Gr", width=2)
        t.add_column("Why", width=42)
        self._repopulate()
        self.set_interval(1.0, self._repopulate)

    def _repopulate(self) -> None:
        app = self.app
        profile = app.profile
        label, desc = PROFILE_META[profile]

        head = Text()
        head.append("★ Region Advisor   ", style="bold #7aa2f7")
        head.append("profile: ", style="#7a7a8c")
        head.append(label, style="bold #c9a0dc")
        head.append(f"  ·  {desc}", style="#7a7a8c")
        self.query_one("#advisor-head", Static).update(head)

        # best endpoint per region (country)
        by_country: dict[str, tuple] = {}
        for mon in app.monitors:
            sc = score(mon.stats, profile)
            c = mon.target.country
            cur = by_country.get(c)
            if cur is None or (sc or -1.0) > (cur[1] if cur[1] is not None else -1.0):
                by_country[c] = (mon, sc)

        ranked = sorted(
            by_country.values(),
            key=lambda t: (t[1] is None, -(t[1] or 0.0)),
        )

        table = self.query_one("#advisor-table", DataTable)
        table.clear()
        for i, (mon, sc) in enumerate(ranked, start=1):
            rank_style = "bold #3ddc84" if i == 1 else "#9a9ab0"
            rank = Text(str(i), style=rank_style)
            flag = mon.target.flag
            name = Text.assemble((f"{flag} ", ""), (mon.target.country, "bold"))
            if i == 1:
                name.append("  ★", style="bold #3ddc84")
            host = Text(mon.target.host, style="#9a9ab0")
            bar = _score_bar(sc, width=10)
            sc_txt = Text(f" {sc:3.0f}" if sc is not None else "   —", style="bold #e0e0f0")
            bar.append_text(sc_txt)
            g = grade(sc)
            gcolor = {"A": "#3ddc84", "B": "#a6e22e", "C": "#f4bf4f", "D": "#fd8d3c"}.get(g, "#ff3860")
            why = Text(reason(mon.stats, profile), style="#7a7a8c")
            table.add_row(rank, name, host, bar, Text(g, style=f"bold {gcolor}"), why)

        foot = Text(
            "[ ] or p / Tab — switch profile   ·   Esc — close",
            style="#6a6a7c",
        )
        self.query_one("#advisor-foot", Static).update(foot)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_prev_profile(self) -> None:
        i = PROFILES.index(self.app.profile)
        self.app.profile = PROFILES[(i - 1) % len(PROFILES)]
        self._repopulate()

    def action_next_profile(self) -> None:
        i = PROFILES.index(self.app.profile)
        self.app.profile = PROFILES[(i + 1) % len(PROFILES)]
        self._repopulate()


class TracerouteScreen(ModalScreen):
    """mtr-style hop-by-hop path to a single target."""

    BINDINGS = [Binding("escape,q,enter", "close", "Close")]

    def __init__(self, target: Target) -> None:
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-dialog"):
            yield Static(id="trace-head")
            yield DataTable(id="trace-table", cursor_type="row", zebra_stripes=True)
            yield Static(id="trace-foot")

    def on_mount(self) -> None:
        head = Text()
        head.append("⇄ Traceroute   ", style="bold #7aa2f7")
        head.append(f"{self.target.flag} {self.target.country}   ", style="bold")
        head.append(self.target.host, style="#9a9ab0")
        self.query_one("#trace-head", Static).update(head)

        t = self.query_one("#trace-table", DataTable)
        t.add_column("Hop", key="hop", width=3)
        t.add_column("Host / IP", key="host", width=22)
        t.add_column("Loss", key="loss", width=5)
        t.add_column("Probes (ms)", key="probes", width=24)
        t.add_column("Best", key="best", width=5)
        t.add_column("Location", key="loc", width=34)
        self.query_one("#trace-foot", Static).update(
            Text("running…   ·   Esc close", style="#6a6a7c")
        )
        self.run_worker(self._run(), exclusive=True)

    def _initial_loc(self, ip: str) -> Text:
        if ip == "*":
            return Text("—", style="#3a3a4a")
        if is_global_ip(ip):
            return Text("…", style="#6a6a7c")
        return Text("private network", style="#6a6a7c")

    async def _run(self) -> None:
        table = self.query_one("#trace-table", DataTable)
        async for hop in traceroute(self.target.host, max_hops=24, queries=3):
            if "error" in hop:
                self.query_one("#trace-foot", Static).update(
                    Text(hop["error"], style="bold #ff3860")
                )
                return
            ok = [t for t in hop["times"] if t is not None]
            best = min(ok) if ok else None
            probes = Text()
            for t in hop["times"]:
                if t is None:
                    probes.append("*  ", style="bold #ff3860")
                else:
                    probes.append(f"{t:.1f} ", style=latency_color(t))
            loss = hop["loss"]
            loss_txt = (
                Text("0%", style="#3ddc84")
                if loss <= 0
                else Text(f"{loss:.0f}%", style="bold #ff3860")
            )
            ip = hop["host"]
            host_style = "#9a9ab0" if ip != "*" else "#ff3860"
            rowkey = f"hop-{hop['hop']}"
            table.add_row(
                Text(str(hop["hop"]), style="#7a7a8c"),
                Text(ip, style=host_style),
                loss_txt,
                probes,
                latency_text(best),
                self._initial_loc(ip),
                key=rowkey,
            )
            if is_global_ip(ip):
                self.run_worker(self._geo_hop(rowkey, ip), group="trace-geo")
        self.query_one("#trace-foot", Static).update(
            Text("done   ·   Esc close", style="#6a6a7c")
        )

    async def _geo_hop(self, rowkey: str, ip: str) -> None:
        data = await geo_lookup(ip)
        if not data:
            return
        flag = flag_emoji(data.get("countryCode"))
        place = data.get("city") or data.get("country") or ip
        asn = (data.get("as") or "").split(" ", 1)[0]
        txt = Text(f"{flag} {place}", style="#9a9ab0")
        if asn.startswith("AS"):
            txt.append(f"  {asn}", style="#7a7a8c")
        try:
            self.query_one("#trace-table", DataTable).update_cell(
                rowkey, "loc", txt, update_width=False
            )
        except Exception:
            pass

    def action_close(self) -> None:
        self.dismiss(None)


class PingMonApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "pingmon"
    SUB_TITLE = "availability monitor by country"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("m,M", "sort_latency", "By ms"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("g", "advisor", "Advisor"),
        Binding("p", "cycle_profile", "Profile"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("enter", "traceroute", "Trace"),
        Binding("a", "add_target", "Add"),
        Binding("A", "toggle_alerts", "Alerts on/off"),
        Binding("E", "edit_target", "Edit"),
        Binding("d", "delete_target", "Delete"),
        Binding("r", "reset_stats", "Reset"),
        Binding("e", "open_config", "Config"),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
    ]

    SORT_MODES = ["country", "latency", "loss", "jitter"]
    SORT_LABELS = {
        "country": "by country",
        "latency": "by latency",
        "loss": "by loss",
        "jitter": "by jitter",
    }

    def __init__(self) -> None:
        super().__init__()
        self.cfg: Config = load_config()
        self.monitors: list[Monitor] = [
            Monitor(t, self.cfg.history) for t in self.cfg.targets
        ]
        self.order: list[Monitor] = list(self.monitors)
        self.paused = False
        self.sort_mode = "country"
        self.latency_desc = False  # False = fastest first, True = slowest first
        self.profile = "voip"      # Region Advisor use-case profile
        self.filter_mode = "all"   # all | mine | others
        self.frame = 0
        self.selected: Monitor | None = None
        self.alerting: set[str] = set()  # keys of targets currently in alert
        self.alerts_enabled = True       # master switch for the alert system

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield DataTable(id="table", zebra_stripes=True, cursor_type="row")
            with VerticalScroll(id="detail"):
                yield Static(id="detail-title")
                yield Static(id="detail-meta")
                yield Label("Latency · recent samples", classes="detail-h")
                yield Sparkline([0], id="detail-spark", summary_function=max)
                yield Static(id="detail-quality")
                yield Label("Distribution · min — median — max", classes="detail-h")
                yield Static(id="detail-dist")
                yield Static(id="detail-distsum")
                yield Static(id="detail-stats")
                yield Static(id="detail-hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column("", key="status", width=2)
        table.add_column("Country", key="country", width=16)
        table.add_column("Host", key="host", width=26)
        table.add_column(Text("ms", justify="right"), key="last", width=5)
        table.add_column(Text("avg", justify="right"), key="avg", width=5)
        table.add_column(Text("loss", justify="right"), key="loss", width=5)
        table.add_column("trend", key="trend", width=20)

        for mon in self.order:
            table.add_row(*self._row_cells(mon), key=mon.key)

        for i, mon in enumerate(self.monitors):
            delay = (self.cfg.interval / max(1, len(self.monitors))) * i
            mon.worker = self.run_worker(
                self._ping_loop(mon, delay), name=mon.key, group="ping"
            )

        if self.order:
            self.selected = self.order[0]
        self.set_interval(0.2, self._tick)
        self._tick()

    # ---------- ping engine ----------

    async def _ping_loop(self, mon: Monitor, delay: float) -> None:
        await asyncio.sleep(delay)
        mon.ip = await resolve(mon.target.host)
        if mon.geo is None:
            mon.geo = await geo_lookup(mon.target.host)
            if mon.geo and not mon.ip:
                mon.ip = mon.geo.get("query")
        while True:
            if not self.paused:
                lat = await tcp_ping(
                    mon.target.host, mon.target.port, self.cfg.timeout
                )
                mon.stats.record(lat)
            await asyncio.sleep(self.cfg.interval)

    # ---------- row rendering ----------

    def _row_cells(self, mon: Monitor) -> list:
        st = mon.stats
        name = Text.assemble((f"{mon.target.flag} ", ""), (mon.target.country, "bold"))
        glyph_color = STATUS_META[st.status][1]
        glyph_style = f"bold {glyph_color}"
        if mon.key in self.alerting:
            glyph_style = f"blink bold {glyph_color}"
        glyph = Text(STATUS_META[st.status][2], style=glyph_style)
        return [
            glyph,
            name,
            Text(mon.target.host, style="#9a9ab0"),
            latency_text(st.last),
            latency_text(st.avg),
            loss_text(st.loss),
            sparkline(st.samples, width=20),
        ]

    def _update_row(self, mon: Monitor) -> None:
        table = self.query_one("#table", DataTable)
        cells = self._row_cells(mon)
        keys = ["status", "country", "host", "last", "avg", "loss", "trend"]
        for col, val in zip(keys, cells):
            try:
                table.update_cell(mon.key, col, val, update_width=False)
            except Exception:
                pass

    # ---------- periodic tick ----------

    def _tick(self) -> None:
        self.frame += 1
        self._check_alerts()
        for mon in self.monitors:
            self._update_row(mon)
        self._update_banner()
        self._update_detail()

    # ---------- alerts ----------

    def _is_bad(self, mon: Monitor) -> bool:
        st = mon.stats
        cfg = self.cfg
        if st.consecutive_fail >= cfg.alert_window:
            return True
        recent = list(st.samples)[-max(cfg.alert_window, 10):]
        if cfg.alert_latency > 0 and len(recent) >= cfg.alert_window:
            tail = recent[-cfg.alert_window:]
            if all((s is None or s > cfg.alert_latency) for s in tail):
                return True
        if cfg.alert_loss > 0 and len(recent) >= cfg.alert_window:
            rloss = 100.0 * sum(1 for s in recent if s is None) / len(recent)
            if rloss >= cfg.alert_loss:
                return True
        return False

    def _check_alerts(self) -> None:
        if not self.alerts_enabled:
            if self.alerting:
                self.alerting.clear()
            return
        for mon in self.monitors:
            key = mon.key
            bad = self._is_bad(mon)
            if bad and key not in self.alerting:
                self.alerting.add(key)
                self._raise_alert(mon)
            elif not bad and key in self.alerting:
                self.alerting.discard(key)
                self.notify(
                    f"Recovered · {mon.target.country} ({mon.target.host})",
                    timeout=3,
                )

    def _raise_alert(self, mon: Monitor) -> None:
        st = mon.stats
        if st.status == "DOWN" or st.last is None:
            detail = "unreachable"
        else:
            detail = f"{st.last:.0f}ms · loss {st.loss:.0f}%"
        self.bell()
        self.notify(
            f"ALERT · {mon.target.country} ({mon.target.host}) — {detail}",
            severity="error",
            timeout=6,
        )
        if self.cfg.desktop_notify:
            self.run_worker(
                desktop_notify(
                    "pingmon alert",
                    f"{mon.target.country} ({mon.target.host}) — {detail}",
                ),
                group="notify",
            )

    def _update_banner(self) -> None:
        total = len(self.monitors)
        up = sum(1 for m in self.monitors if m.stats.status not in ("DOWN", "PENDING"))
        down = sum(1 for m in self.monitors if m.stats.status == "DOWN")
        oks = [m.stats.last for m in self.monitors if m.stats.last is not None]
        best = min(oks) if oks else None
        avg = sum(oks) / len(oks) if oks else None

        spin = SPINNER[self.frame % len(SPINNER)]
        live = Text(
            f" {spin} PAUSED " if self.paused else f" {spin} LIVE ",
            style="bold #1a1a24 on #f4bf4f" if self.paused else "bold #1a1a24 on #3ddc84",
        )

        b = Text()
        b.append(" ⚡ pingmon ", style="bold #1a1a24 on #7aa2f7")
        b.append("  ")
        b.append_text(live)
        b.append("    online ", style="#7a7a8c")
        b.append(f"{up}/{total}", style="bold #3ddc84")
        b.append("  ·  down ", style="#7a7a8c")
        b.append(str(down), style="bold #ff3860" if down else "bold #3ddc84")
        b.append("  ·  best ", style="#7a7a8c")
        b.append_text(latency_text(best, "ms"))
        b.append("  ·  avg ", style="#7a7a8c")
        b.append_text(latency_text(avg, "ms"))
        b.append("  ·  sort ", style="#7a7a8c")
        sort_label = self.SORT_LABELS[self.sort_mode]
        if self.sort_mode == "latency":
            sort_label += " ↑" if not self.latency_desc else " ↓"
        b.append(sort_label, style="#c9a0dc")

        if self.filter_mode != "all":
            b.append("  ·  filter ", style="#7a7a8c")
            b.append(self.FILTER_LABELS[self.filter_mode], style="bold #7aa2f7")

        if not self.alerts_enabled:
            b.append("  ·  ⚲ alerts off", style="bold #7a7a8c")
        elif self.alerting:
            b.append(f"  ·  ⚠ {len(self.alerting)} alert", style="bold #ff3860")

        best, best_score = self._best_for_profile()
        if best is not None:
            b.append("  ·  ★ ", style="#7a7a8c")
            b.append(PROFILE_META[self.profile][0], style="#7a7a8c")
            b.append(f": {best.target.flag} {best.target.country}", style="bold #3ddc84")
        b.append("  ·  " + datetime.now().strftime("%H:%M:%S"), style="#7a7a8c")
        self.query_one("#banner", Static).update(b)

    def _best_for_profile(self) -> tuple[Monitor | None, float]:
        best: Monitor | None = None
        best_score = -1.0
        for mon in self.monitors:
            sc = score(mon.stats, self.profile)
            if sc is None:
                continue
            if sc > best_score:
                best_score = sc
                best = mon
        return best, best_score

    def _update_detail(self) -> None:
        mon = self.selected
        if mon is None:
            return
        st = mon.stats

        title = Text()
        title.append(f"{mon.target.flag}  ")
        title.append(mon.target.country, style="bold #e0e0f0")
        title.append("    ")
        label, color, glyph = STATUS_META[st.status]
        title.append(f"{glyph} {label}", style=f"bold {color}")
        self.query_one("#detail-title", Static).update(title)

        meta = Text()
        meta.append("host  ", style="#7a7a8c")
        meta.append(f"{mon.target.host}:{mon.target.port}\n", style="#9a9ab0")
        meta.append("addr  ", style="#7a7a8c")
        meta.append(f"{mon.ip or '…'}", style="#9a9ab0")
        if mon.geo:
            city = mon.geo.get("city")
            region = mon.geo.get("regionName")
            loc = ", ".join(p for p in (city, region) if p)
            if loc:
                meta.append("\nloc   ", style="#7a7a8c")
                meta.append(loc, style="#9a9ab0")
            net = mon.geo.get("as") or mon.geo.get("isp")
            if net:
                meta.append("\nnet   ", style="#7a7a8c")
                meta.append(net, style="#9a9ab0")
        self.query_one("#detail-meta", Static).update(meta)

        spark = self.query_one("#detail-spark", Sparkline)
        ok = st.ok_values
        spark.data = ok[-60:] if ok else [0]

        q = Text("quality  ", style="#7a7a8c")
        q.append_text(quality_bar(st.last, width=24))
        self.query_one("#detail-quality", Static).update(q)

        self.query_one("#detail-dist", Static).update(distribution_strip(st.samples, width=34))
        self.query_one("#detail-distsum", Static).update(self._dist_summary(st))

        self.query_one("#detail-stats", Static).update(self._stats_block(st))

        hint = Text(
            "[g] advisor   [p] profile   [f] filter   [enter] traceroute   [m] by ms   [space] pause\n"
            "[A] alerts on/off   [s] sort   [a] add   [E] edit   [d] delete   [r] reset   [e] config   [q] quit",
            style="#6a6a7c",
        )
        self.query_one("#detail-hint", Static).update(hint)

    def _dist_summary(self, st: TargetStats) -> Text:
        out = Text()
        for label, val in (
            ("min", st.vmin),
            ("med", st.median),
            ("p90", st.percentile(90.0)),
            ("max", st.vmax),
        ):
            out.append(f"{label} ", style="#7a7a8c")
            out.append_text(latency_text(val))
            out.append("  ")
        return out

    def _stats_block(self, st: TargetStats) -> Text:
        def row(name: str, value: Text) -> Text:
            t = Text(f"{name:<9}", style="#7a7a8c")
            t.append_text(value)
            t.append("\n")
            return t

        out = Text()
        out.append_text(row("current", latency_text(st.last, " ms")))
        out.append_text(row("min", latency_text(st.vmin, " ms")))
        out.append_text(row("avg", latency_text(st.avg, " ms")))
        out.append_text(row("max", latency_text(st.vmax, " ms")))
        jit = (
            Text(f"{st.jitter:.0f} ms", style="#c9a0dc")
            if st.jitter is not None
            else Text("—", style="#555566")
        )
        out.append_text(row("jitter", jit))
        out.append_text(row("loss", loss_text(st.loss)))
        mos = st.mos
        if mos is None:
            mos_txt = Text("—", style="#555566")
        else:
            mcolor = (
                "#3ddc84" if mos >= 4.0
                else "#a6e22e" if mos >= 3.6
                else "#f4bf4f" if mos >= 3.0
                else "#ff3860"
            )
            mos_txt = Text(f"{mos:.2f} / 4.5", style=f"bold {mcolor}")
        out.append_text(row("MOS", mos_txt))
        out.append_text(
            row("samples", Text(f"{st.sent}  (lost {st.lost})", style="#9a9ab0"))
        )
        return out

    # ---------- selection ----------

    @on(DataTable.RowHighlighted)
    def _on_highlight(self, event: DataTable.RowHighlighted) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self.order):
            self.selected = self.order[idx]
            self._update_detail()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter (or click) on a row opens the traceroute drill-down.
        idx = event.cursor_row
        if 0 <= idx < len(self.order):
            self.selected = self.order[idx]
            self.push_screen(TracerouteScreen(self.selected.target))

    # ---------- actions ----------

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        self.notify("Paused" if self.paused else "Resumed", timeout=1.5)

    def action_toggle_alerts(self) -> None:
        self.alerts_enabled = not self.alerts_enabled
        if not self.alerts_enabled:
            self.alerting.clear()
        self.notify(
            "Alerts enabled" if self.alerts_enabled else "Alerts disabled",
            timeout=1.5,
        )
        self._tick()

    def action_cycle_sort(self) -> None:
        i = self.SORT_MODES.index(self.sort_mode)
        self.sort_mode = self.SORT_MODES[(i + 1) % len(self.SORT_MODES)]
        self._resort()
        self.notify(f"Sort: {self.SORT_LABELS[self.sort_mode]}", timeout=1.5)

    def action_sort_latency(self) -> None:
        # press once -> fastest first; press again -> slowest first
        if self.sort_mode == "latency":
            self.latency_desc = not self.latency_desc
        else:
            self.sort_mode = "latency"
            self.latency_desc = False
        self._resort()
        which = "slowest first" if self.latency_desc else "fastest first"
        self.notify(f"Sort: latency · {which}", timeout=1.5)

    def _sort_key(self, mon: Monitor):
        st = mon.stats
        big = float("inf")
        last = st.last
        if self.sort_mode == "country":
            return (mon.target.country, mon.target.host)
        if self.sort_mode == "latency":
            if not self.latency_desc:
                # fastest first; unreachable sinks to the bottom
                return (1 if last is None else 0, last if last is not None else 0.0, mon.target.host)
            # slowest first; unreachable rises to the top (worst case)
            return (0 if last is None else 1, -(last or 0.0), mon.target.host)
        if self.sort_mode == "loss":
            return (-st.loss, last if last is not None else big)
        if self.sort_mode == "jitter":
            return (-(st.jitter or 0.0),)
        return (mon.target.country,)

    def _passes_filter(self, mon: Monitor) -> bool:
        if self.filter_mode == "mine":
            return mon.target.source == "user"
        if self.filter_mode == "others":
            return mon.target.source != "user"
        return True

    def _resort(self) -> None:
        table = self.query_one("#table", DataTable)
        ordered = sorted(self.monitors, key=self._sort_key)
        self.order = [m for m in ordered if self._passes_filter(m)]
        sel_key = self.selected.key if self.selected else None
        table.clear()
        for mon in self.order:
            table.add_row(*self._row_cells(mon), key=mon.key)
        # keep a valid selection within the visible rows
        if self.order and (self.selected not in self.order):
            self.selected = self.order[0]
            sel_key = self.selected.key
        if sel_key:
            try:
                table.move_cursor(row=table.get_row_index(sel_key))
            except Exception:
                pass

    def action_reset_stats(self) -> None:
        for mon in self.monitors:
            mon.stats.reset()
        self.notify("Statistics reset", timeout=1.5)
        self._tick()

    def action_add_target(self) -> None:
        def handle(target: Target | None) -> None:
            if target is None:
                return
            mon = Monitor(target, self.cfg.history)
            self.monitors.append(mon)
            self.cfg.targets.append(target)
            save_config(self.cfg)
            mon.worker = self.run_worker(
                self._ping_loop(mon, 0.0), name=mon.key, group="ping"
            )
            self._resort()
            self.notify(f"Added: {target.host}", timeout=2)
            self._maybe_autodetect(mon)

        self.push_screen(TargetFormScreen(title="New target"), handle)

    def _maybe_autodetect(self, mon: Monitor) -> None:
        """Fill country/flag from GeoIP when the user left them blank."""
        t = mon.target
        if t.country and t.flag:
            return
        if not t.country:
            t.country = "…"
        if not t.flag:
            t.flag = "🏳"
        self._resort()
        self.run_worker(self._autodetect(mon), group="geo")

    async def _autodetect(self, mon: Monitor) -> None:
        data = await geo_lookup(mon.target.host)
        t = mon.target
        if data:
            mon.geo = data
            if t.country in ("", "…"):
                t.country = data.get("country") or t.host
            if t.flag in ("", "🏳"):
                t.flag = flag_emoji(data.get("countryCode"))
            self.cfg.targets = [m.target for m in self.monitors]
            save_config(self.cfg)
            city = data.get("city")
            where = f"{city}, {t.country}" if city else t.country
            self.notify(f"Detected · {t.flag} {where}", timeout=2)
        else:
            if t.country in ("", "…"):
                t.country = t.host
            self.notify("GeoIP detection failed", severity="warning", timeout=2)
        self._resort()

    def action_edit_target(self) -> None:
        mon = self.selected
        if mon is None:
            return

        def handle(target: Target | None) -> None:
            if target is None:
                return
            old = mon.target
            mon.target = target
            self.cfg.targets = [m.target for m in self.monitors]
            save_config(self.cfg)
            # if the endpoint changed, reset stats and restart the probe worker
            if (target.host, target.port) != (old.host, old.port):
                self.alerting.discard(old.key)
                mon.stats.reset()
                mon.ip = None
                if mon.worker is not None:
                    mon.worker.cancel()
                mon.worker = self.run_worker(
                    self._ping_loop(mon, 0.0), name=mon.key, group="ping"
                )
            self._resort()
            self.notify(f"Updated: {target.host}", timeout=2)
            self._maybe_autodetect(mon)

        self.push_screen(
            TargetFormScreen(initial=mon.target, title="Edit target"), handle
        )

    def action_delete_target(self) -> None:
        mon = self.selected
        if mon is None or len(self.monitors) <= 1:
            self.notify("Cannot delete the last target", severity="warning", timeout=2)
            return
        if mon.worker is not None:
            mon.worker.cancel()
        self.monitors.remove(mon)
        self.cfg.targets = [m.target for m in self.monitors]
        save_config(self.cfg)
        self.selected = self.monitors[0]
        self._resort()
        self.notify(f"Removed: {mon.target.country}", timeout=2)

    def action_open_config(self) -> None:
        self.notify(f"Config file: {self.cfg.path}", timeout=4)

    def action_advisor(self) -> None:
        self.push_screen(AdvisorScreen())

    FILTER_MODES = ["all", "mine", "others"]
    FILTER_LABELS = {"all": "all", "mine": "mine only", "others": "others only"}

    def action_cycle_filter(self) -> None:
        i = self.FILTER_MODES.index(self.filter_mode)
        self.filter_mode = self.FILTER_MODES[(i + 1) % len(self.FILTER_MODES)]
        self._resort()
        self.notify(f"Filter: {self.FILTER_LABELS[self.filter_mode]}", timeout=1.5)

    def action_cycle_profile(self) -> None:
        i = PROFILES.index(self.profile)
        self.profile = PROFILES[(i + 1) % len(PROFILES)]
        self.notify(f"Profile: {PROFILE_META[self.profile][0]}", timeout=1.5)

    def action_traceroute(self) -> None:
        if self.selected is not None:
            self.push_screen(TracerouteScreen(self.selected.target))

    def action_cursor_up(self) -> None:
        self.query_one("#table", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#table", DataTable).action_cursor_down()


def main() -> None:
    import argparse
    import os

    from . import __version__

    parser = argparse.ArgumentParser(
        prog="pingmon",
        description="TUI monitor of latency and availability to servers by country.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"pingmon {__version__}"
    )
    parser.add_argument(
        "-c", "--config", metavar="PATH",
        help="path to config.toml (overrides $PINGMON_CONFIG)",
    )
    args = parser.parse_args()
    if args.config:
        os.environ["PINGMON_CONFIG"] = args.config

    PingMonApp().run()


if __name__ == "__main__":
    main()
