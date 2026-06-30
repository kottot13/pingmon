"""pingmon TUI: live latency & availability monitoring per country."""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import replace
from datetime import datetime
from time import monotonic

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
    OptionList,
    Static,
)

from .config import Config, Target, load_config, save_config
from .netutil import (
    build_ssh_argv,
    desktop_notify,
    fetch_server_load,
    flag_emoji,
    geo_lookup,
    is_global_ip,
    ssh_agent_has_keys,
    traceroute,
)
from .pinger import resolve, tcp_ping, tcp_ping_banner
from .render import (
    STATUS_META,
    country_label,
    distribution_block,
    distribution_caption,
    latency_chart,
    latency_color,
    latency_text,
    loss_text,
    quality_block,
    sparkline,
)
from .scoring import PROFILE_META, PROFILES, grade, reason, score
from .stats import TargetStats
from .terminal import TerminalPane

# Remote diagnostics offered by the `l` menu. Each entry runs over SSH in the
# right console. `tui` entries are full-screen programs (exit with their own key
# or Ctrl-]); the rest are one-shot reports piped to `less` (exit with q). Every
# command sticks to tools shipped almost everywhere (coreutils / util-linux /
# procps / iproute2 / systemd) and degrades gracefully when a nicer tool is
# missing — important because servers are mostly Linux of varying minimalism.
DIAG_MENU = [
    {"key": "top", "label": "top — interactive processes (always present)", "tui": True,
     "cmd": "exec top"},
    {"key": "load", "label": "Load average — why is it high (CPU/IO/top procs)", "tui": False,
     "cmd": (
        "echo '== load average =='; uptime; echo; "
        "echo '== cpu & io: r=runnable b=blocked(IO) wa=iowait =='; vmstat 1 4; echo; "
        "echo '== memory =='; (free -h 2>/dev/null || free); echo; "
        "echo '== top by CPU =='; (ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu 2>/dev/null | head -12 || ps aux | sort -rk3 | head -12); echo; "
        "echo '== top by MEM =='; (ps -eo pid,user,pcpu,pmem,comm --sort=-pmem 2>/dev/null | head -12); echo; "
        "echo '== per-disk I/O (iostat) =='; (iostat -x 1 2 2>/dev/null || echo 'iostat not installed (apt install sysstat)')"
     )},
    {"key": "logins", "label": "Logins & intrusion — who, history, failed", "tui": False,
     "cmd": (
        "echo '== logged in now =='; w; echo; "
        "echo '== recent logins =='; last -n 25; echo; "
        "echo '== failed logins (btmp) =='; (lastb -n 25 2>/dev/null || sudo -n lastb -n 25 2>/dev/null || echo '(need root to read /var/log/btmp)')"
     )},
    {"key": "ssh", "label": "SSH auth log — accepted & brute-force", "tui": False,
     "cmd": (
        "echo '== recent sshd auth =='; "
        "(journalctl -u ssh -u sshd --no-pager -n 200 2>/dev/null || grep -hE 'sshd' /var/log/auth.log /var/log/secure 2>/dev/null | tail -n 200 || echo 'no journal/auth log access'); echo; "
        "echo '== failed passwords by source IP =='; "
        "(journalctl -u ssh -u sshd --no-pager 2>/dev/null || cat /var/log/auth.log /var/log/secure 2>/dev/null) | grep -i 'failed password' | grep -oE 'from [0-9.]+' | sort | uniq -c | sort -rn | head -15"
     )},
    {"key": "conns", "label": "Connections & ports — listening / established", "tui": False,
     "cmd": (
        "echo '== listening sockets =='; (ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null || netstat -tln); echo; "
        "echo '== established connections =='; (ss -tunp 2>/dev/null || netstat -tunp 2>/dev/null)"
     )},
    {"key": "kernel", "label": "Kernel & system log — dmesg / warnings", "tui": False,
     "cmd": (
        "echo '== dmesg (last 60) =='; (dmesg -T 2>/dev/null | tail -n 60 || dmesg | tail -n 60); echo; "
        "echo '== journal warnings+ this boot =='; (journalctl -p warning -b --no-pager -n 80 2>/dev/null || echo 'journalctl not available')"
     )},
    {"key": "nettraf", "label": "Live network traffic — iftop/nload (or counters)", "tui": True,
     "cmd": (
        "if command -v iftop >/dev/null 2>&1; then exec iftop; fi; "
        "if command -v nethogs >/dev/null 2>&1; then exec nethogs; fi; "
        "if command -v nload >/dev/null 2>&1; then exec nload; fi; "
        "if command -v bmon >/dev/null 2>&1; then exec bmon; fi; "
        "echo 'No iftop/nethogs/nload/bmon installed — showing live interface counters (Ctrl-] to exit).'; sleep 1; "
        "exec watch -n1 'ip -s link'"
     )},
    {"key": "ifstat", "label": "Interface stats — packets/bytes/errors/drops", "tui": False,
     "cmd": (
        "echo '== interface counters =='; (ip -s link 2>/dev/null || netstat -i); echo; "
        "echo '== socket summary =='; (ss -s 2>/dev/null || echo 'ss not available')"
     )},
    {"key": "disk", "label": "Disk & I/O — usage, block devices, biggest dirs", "tui": False,
     "cmd": (
        "echo '== filesystem usage =='; df -h; echo; "
        "echo '== block devices =='; (lsblk 2>/dev/null || echo 'lsblk not available'); echo; "
        "echo '== biggest dirs under / (one level) =='; (du -xhd1 / 2>/dev/null | sort -rh | head -15 || echo)"
     )},
]
DIAG_KEYS = [e["key"] for e in DIAG_MENU]

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class NavDataTable(DataTable):
    """DataTable that lets ←/→ bubble up so the app can switch panels.

    The row cursor only moves vertically here, so the built-in horizontal
    cursor bindings are disabled; the arrows then reach the app's panel-focus
    bindings instead of being swallowed by the table.
    """

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("cursor_left", "cursor_right"):
            return False
        return True


class NavScroll(VerticalScroll):
    """VerticalScroll that lets ←/→ bubble up for panel switching."""

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("scroll_left", "scroll_right"):
            return False
        return True


class Monitor:
    """A target bundled with its stats and background ping worker."""

    def __init__(self, target: Target, history: int) -> None:
        self.target = target
        self.stats = TargetStats(maxlen=history)
        self.ip: str | None = None
        self.geo: dict | None = None  # city / ISP / ASN from GeoIP
        self.worker = None
        self.load: dict | None = None   # live server load snapshot (servers only)
        self.load_ts: float | None = None
        self.load_busy = False

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
                placeholder="Country code, e.g. PL (optional)", id="in-flag",
                value=(init.code if init and init.code != "??" else ""),
            )
            yield Input(
                placeholder="Host or IP (e.g. 1.1.1.1)", id="in-host",
                value=init.host if init else "",
            )
            yield Input(
                placeholder="Port (default 443; 22 for SSH)", id="in-port",
                value=str(init.port) if init else "443",
            )
            yield Input(
                placeholder="SSH user (blank = plain monitor)", id="in-ssh",
                value=(init.ssh_user if init else ""),
            )
            yield Label(
                "Leave country/code empty to auto-detect (GeoIP). "
                "Set an SSH user to make this your server (Enter logs in, l = top).",
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
        # empty country/code are kept empty so the app can auto-detect via GeoIP
        country = self.query_one("#in-country", Input).value.strip()
        # the field accepts a 2-letter ISO code (turned into the stored flag) or,
        # for flag-capable terminals, a flag emoji pasted directly; blank = detect.
        raw = self.query_one("#in-flag", Input).value.strip()
        flag = flag_emoji(raw) if (len(raw) == 2 and raw.isalpha()) else raw
        port_raw = self.query_one("#in-port", Input).value.strip() or "443"
        try:
            port = int(port_raw)
        except ValueError:
            port = 443
        ssh_user = self.query_one("#in-ssh", Input).value.strip()
        top_tool = self.initial.top_tool if self.initial else ""
        self.dismiss(Target(
            country=country, flag=flag, host=host, port=port, source="user",
            ssh_user=ssh_user, top_tool=top_tool,
        ))


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
            name = country_label(mon.target.code, mon.target.country)
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
        head.append(f"{self.target.code}  {self.target.country}   ", style="bold")
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
        any_reply = False
        hops_seen = 0
        async for hop in traceroute(self.target.host, self.target.port, max_hops=24, queries=3):
            if "error" in hop:
                self.query_one("#trace-foot", Static).update(
                    Text(hop["error"], style="bold #ff3860")
                )
                return
            hops_seen += 1
            ok = [t for t in hop["times"] if t is not None]
            if ok:
                any_reply = True
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

        if hops_seen == 0:
            foot = Text("no route data — is `traceroute` installed?", style="bold #ff3860")
        elif not any_reply:
            # every hop timed out: the host/network drops traceroute probes
            foot = Text(
                "no hops replied — this host or its network blocks traceroute "
                "(common on VPS). The service can still be reachable.   ·   Esc close",
                style="#f4bf4f",
            )
        else:
            foot = Text("done   ·   Esc close", style="#6a6a7c")
        self.query_one("#trace-foot", Static).update(foot)

    async def _geo_hop(self, rowkey: str, ip: str) -> None:
        data = await geo_lookup(ip)
        if not data:
            return
        code = (data.get("countryCode") or "??").upper()
        place = data.get("city") or data.get("country") or ip
        asn = (data.get("as") or "").split(" ", 1)[0]
        txt = Text(f"{code}  {place}", style="#9a9ab0")
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


class DiagnosticsScreen(ModalScreen[str | None]):
    """Scrollable menu of remote diagnostics; returns the chosen entry's key."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, entries: list[dict], default: str = "") -> None:
        super().__init__()
        self.entries = entries
        self.keys = [e["key"] for e in entries]
        self.default = default if default in self.keys else self.keys[0]

    def compose(self) -> ComposeResult:
        with Vertical(id="diag-dialog"):
            yield Label("Remote diagnostics", id="diag-title")
            yield Label(
                "↑/↓ choose · Enter runs · exit later with q (or Ctrl-])",
                id="diag-hint",
            )
            yield OptionList(*[e["label"] for e in self.entries], id="diag-list")

    def on_mount(self) -> None:
        ol = self.query_one("#diag-list", OptionList)
        ol.highlighted = self.keys.index(self.default)
        ol.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected)
    def _pick(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.keys[event.option_index])


class CustomToolScreen(ModalScreen[tuple[str, bool] | None]):
    """Ask for a tool to run on the server and whether to install it if missing."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="custom-dialog"):
            yield Label("Run a tool on the server", id="custom-title")
            yield Input(placeholder="Tool name (e.g. htop, btop, iotop, ncdu, glances)", id="in-tool")
            yield Label(
                "‘Install & run’ installs it first if missing (sudo may be asked "
                "in the console). Added tools stay in this menu.",
                id="custom-hint",
            )
            with Horizontal(id="custom-buttons"):
                yield Button("Install & run", variant="success", id="c-install")
                yield Button("Run only", variant="default", id="c-run")
                yield Button("Cancel", variant="default", id="c-cancel")

    def on_mount(self) -> None:
        self.query_one("#in-tool", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#c-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#c-install")
    def _install(self) -> None:
        self._done(True)

    @on(Button.Pressed, "#c-run")
    def _run(self) -> None:
        self._done(False)

    def _done(self, install: bool) -> None:
        raw = self.query_one("#in-tool", Input).value.strip()
        tool = raw.split()[0] if raw else ""
        if not tool:
            self.query_one("#in-tool", Input).focus()
            return
        self.dismiss((tool, install))


class SshSetupScreen(ModalScreen[tuple[str, int] | None]):
    """Ask for the SSH user + port to turn a target into a server you can log in to."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, host: str, default_user: str = "", default_port: int = 22) -> None:
        super().__init__()
        self.host = host
        self.default_user = default_user
        self.default_port = default_port

    def compose(self) -> ComposeResult:
        with Vertical(id="ssh-dialog"):
            yield Label(f"Log in to {self.host}", id="ssh-title")
            yield Input(placeholder="SSH user (e.g. root)", id="in-ssh-user",
                        value=self.default_user)
            yield Input(placeholder="SSH port (default 22)", id="in-ssh-port",
                        value=str(self.default_port))
            yield Label(
                "Saved on the target so it becomes your server: availability is "
                "then checked over SSH, and l opens server tools (top, load, logins, your own…).",
                id="ssh-hint",
            )
            with Horizontal(id="ssh-buttons"):
                yield Button("Connect", variant="success", id="ssh-ok")
                yield Button("Cancel", variant="default", id="ssh-cancel")

    def on_mount(self) -> None:
        self.query_one("#in-ssh-user", Input).focus()

    @on(Button.Pressed, "#ssh-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#ssh-ok")
    def _submit(self) -> None:
        user = self.query_one("#in-ssh-user", Input).value.strip()
        if not user:
            self.query_one("#in-ssh-user", Input).focus()
            return
        port_raw = self.query_one("#in-ssh-port", Input).value.strip() or "22"
        try:
            port = int(port_raw)
        except ValueError:
            port = 22
        self.dismiss((user, port))


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
        Binding("enter", "login", "SSH"),
        Binding("l", "top", "Tools"),
        Binding("t", "traceroute", "Trace"),
        Binding("a", "add_target", "Add"),
        Binding("A", "toggle_alerts", "Alerts on/off"),
        Binding("E", "edit_target", "Edit"),
        Binding("d", "delete_target", "Delete"),
        Binding("r", "reset_stats", "Reset"),
        Binding("e", "open_config", "Config"),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("shift+left", "focus_left", "Left panel", show=False),
        Binding("shift+right", "focus_right", "Right panel", show=False),
        # Priority so it fires even while a live terminal pane has focus and is
        # forwarding every other key to the remote. This is the always-available
        # way out of an SSH shell or top viewer.
        Binding("ctrl+right_square_bracket", "detach", "Exit console", priority=True),
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
        self.focused_panel = "left"      # which panel ←/→ navigation highlights
        self._session: dict | None = None  # active embedded SSH session metadata
        self.has_agent = False           # ssh-agent/keys present → live load works

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield NavDataTable(id="table", zebra_stripes=True, cursor_type="row")
                yield TerminalPane(id="left-term")
                yield Static(id="left-term-hint", classes="term-hint")
            with Vertical(id="detail"):
                with NavScroll(id="detail-scroll"):
                    yield Static(id="detail-title")
                    yield Static(id="detail-meta")
                    yield Label("Server load · live", classes="detail-h", id="detail-load-h")
                    yield Static(id="detail-load")
                    yield Label("Latency · recent samples", classes="detail-h")
                    yield Static(id="detail-spark")
                    yield Static(id="detail-quality")
                    yield Label("Distribution · how often each latency occurs", classes="detail-h")
                    yield Static(id="detail-dist")
                    yield Static(id="detail-distsum")
                    yield Static(id="detail-stats")
                    yield Static(id="detail-hint")
                yield TerminalPane(id="right-term")
                yield Static(id="right-term-hint", classes="term-hint")
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
        self._update_focus()
        self.has_agent = ssh_agent_has_keys()
        self.set_interval(2.0, self._refresh_selected_load)

    def on_unmount(self) -> None:
        # Don't leave orphaned ssh/htop children behind on exit.
        for sel in ("#left-term", "#right-term"):
            try:
                self.query_one(sel, TerminalPane).stop()
            except Exception:
                pass

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
                # Servers are probed by reading the SSH banner, so a host that
                # only completes the TCP handshake (e.g. under a DDoS scrubber)
                # reads DOWN instead of a false UP. Plain targets keep TCP-connect.
                probe = tcp_ping_banner if mon.target.server else tcp_ping
                lat = await probe(
                    mon.target.host, mon.target.port, self.cfg.timeout
                )
                mon.stats.record(lat)
            await asyncio.sleep(self.cfg.interval)

    # ---------- row rendering ----------

    def _row_cells(self, mon: Monitor) -> list:
        st = mon.stats
        name = country_label(mon.target.code, mon.target.country)
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
            b.append(f": {best.target.code}  {best.target.country}", style="bold #3ddc84")
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

    # ---------- live server load (detail panel) ----------

    def _refresh_selected_load(self) -> None:
        """Poll the selected server's load over SSH every ~8s (key auth only)."""
        mon = self.selected
        if mon is None or not mon.target.server or not self.has_agent:
            return
        if mon.load_busy:
            return
        if mon.load_ts is not None and (monotonic() - mon.load_ts) < 8.0:
            return
        mon.load_busy = True
        self.run_worker(self._load_worker(mon), group="load")

    async def _load_worker(self, mon: Monitor) -> None:
        t = mon.target
        try:
            data = await fetch_server_load(t.ssh_user, t.host, t.port)
        except Exception:
            data = None
        mon.load = data
        mon.load_ts = monotonic()
        mon.load_busy = False
        if mon is self.selected:
            self._update_detail()

    def _server_load_block(self, mon: Monitor) -> Text:
        if not self.has_agent:
            return Text("set up ssh-agent / a key for live load", style="#6a6a7c")
        info = mon.load
        if info is None:
            if mon.load_ts is None:
                return Text("fetching…", style="#6a6a7c")
            return Text("unavailable (needs key auth + Linux)", style="#6a6a7c")

        out = Text()
        la = info.get("la")
        ncpu = info.get("ncpu")
        if la:
            ratio = (la[0] / ncpu) if ncpu else la[0]
            col = (
                "#3ddc84" if ratio < 0.7
                else "#f4bf4f" if ratio < 1.0
                else "#fd8d3c" if ratio < 2.0
                else "#ff3860"
            )
            out.append("load  ", style="#7a7a8c")
            out.append(f"{la[0]:.2f} {la[1]:.2f} {la[2]:.2f}", style=f"bold {col}")
            if ncpu:
                out.append(f"   {ncpu} core{'s' if ncpu != 1 else ''}", style="#7a7a8c")
            if ncpu and ratio >= 1.0:
                out.append("  overloaded", style="bold #ff3860")
            out.append("\n")
        for label, key in (("cpu", "cpu"), ("mem", "mem")):
            procs = info.get(key) or []
            if procs:
                out.append(f"{label}   ", style="#7a7a8c")
                for pct, name in procs[:3]:
                    out.append(f"{pct:.0f}% ", style="bold #c9a0dc")
                    out.append(f"{name}  ", style="#9a9ab0")
                out.append("\n")
        extra = Text()
        dw = info.get("dwait")
        if dw:
            extra.append(f"{dw} task(s) waiting on disk  ", style="bold #fd8d3c")
        rf = info.get("rootfs")
        if rf:
            extra.append(f"·  / {rf}  ", style="#9a9ab0")
        mu, mt = info.get("mem_used"), info.get("mem_total")
        if mu and mt:
            extra.append(f"·  mem {mu}/{mt} MB", style="#9a9ab0")
        if extra.plain.strip():
            out.append_text(extra)
        age = "" if mon.load_ts is None else f"  ({monotonic() - mon.load_ts:.0f}s ago)"
        out.append(age, style="#555566")
        return out

    def _update_detail(self) -> None:
        mon = self.selected
        if mon is None:
            return
        st = mon.stats

        is_server = mon.target.server
        self.query_one("#detail-load-h", Label).display = is_server
        load_widget = self.query_one("#detail-load", Static)
        load_widget.display = is_server
        if is_server:
            load_widget.update(self._server_load_block(mon))

        title = Text()
        title.append(f"{mon.target.code}  ", style="bold #7aa2f7")
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

        self.query_one("#detail-spark", Static).update(
            latency_chart(st.samples, width=44, height=6)
        )

        self.query_one("#detail-quality", Static).update(quality_block(st.last, width=16))

        self.query_one("#detail-dist", Static).update(distribution_block(st, width=30))
        self.query_one("#detail-distsum", Static).update(distribution_caption(st))

        self.query_one("#detail-stats", Static).update(self._stats_block(st))

        hint = Text(
            "[enter] ssh login   [l] tools   [t] traceroute   [ctrl-]] exit console   [shift+←/→] panel   [g] advisor\n"
            "[p] profile   [f] filter   [m] by ms   [space] pause   [A] alerts   [s] sort   [a] add   [E] edit   "
            "[d] delete   [r] reset   [e] config   [q] quit",
            style="#6a6a7c",
        )
        self.query_one("#detail-hint", Static).update(hint)

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
            self._refresh_selected_load()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter (or click) on a row logs in to the server in the left panel.
        idx = event.cursor_row
        if 0 <= idx < len(self.order):
            self.selected = self.order[idx]
            self.action_login()

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
            self.notify(f"Detected · {t.code} {where}", timeout=2)
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

    # ---------- SSH / remote ----------

    def _terminal_live(self) -> bool:
        return any(
            self.query_one(sel, TerminalPane).running
            for sel in ("#left-term", "#right-term")
        )

    def _ensure_server(self, mon: Monitor, then) -> None:
        """Run `then()` once `mon` is a server, prompting for SSH user/port if not."""
        if mon.target.server:
            then()
            return

        def setup(result: tuple[str, int] | None) -> None:
            if result is None:
                return
            user, port = result
            self._promote_to_server(mon, user, port)
            then()

        self.push_screen(
            SshSetupScreen(
                mon.target.host,
                default_user=os.environ.get("USER", ""),
                default_port=22,
            ),
            setup,
        )

    def _promote_to_server(self, mon: Monitor, user: str, ssh_port: int) -> None:
        """Save SSH user/port on the target and restart its probe on the SSH port."""
        old = mon.target
        mon.target = replace(old, ssh_user=user, port=ssh_port, source="user")
        self.cfg.targets = [m.target for m in self.monitors]
        save_config(self.cfg)
        if (mon.target.host, mon.target.port) != (old.host, old.port):
            self.alerting.discard(old.key)
            mon.stats.reset()
            mon.ip = None
            if mon.worker is not None:
                mon.worker.cancel()
            mon.worker = self.run_worker(
                self._ping_loop(mon, 0.0), name=mon.key, group="ping"
            )
        self._resort()

    def action_login(self) -> None:
        """Enter: open a live SSH shell for the selected server (left panel)."""
        if self.query_one("#left-term", TerminalPane).running:
            return  # an SSH shell is already live on the left
        mon = self.selected
        if mon is None:
            return

        def connect() -> None:
            t = mon.target
            argv = build_ssh_argv(t.ssh_user, t.host, t.port)
            if ssh_agent_has_keys():
                self.notify(f"Connecting {t.ssh_user}@{t.host} (ssh-agent)…", timeout=2)
            else:
                self.notify(
                    f"Connecting {t.ssh_user}@{t.host} — type the password in the panel.",
                    timeout=3,
                )
            self._start_terminal("left", argv, mon=mon, retry=self.action_login)

        self._ensure_server(mon, connect)

    def action_top(self) -> None:
        """l: choose htop/atop/top and run it live in the right panel."""
        if self.query_one("#right-term", TerminalPane).running:
            return  # a top viewer is already live on the right
        mon = self.selected
        if mon is None:
            return

        def choose() -> None:
            entries = self._diag_entries()

            def handle(key: str | None) -> None:
                if key is None:
                    return
                if key == "add_tool":
                    self._add_custom_tool(mon)
                    return
                t = mon.target
                t.top_tool = key
                self.cfg.targets = [m.target for m in self.monitors]
                save_config(self.cfg)
                remote = self._diag_command(key)
                argv = build_ssh_argv(t.ssh_user, t.host, t.port, remote_cmd=remote)
                self._start_terminal("right", argv, mon=mon, retry=self.action_top)

            self.push_screen(
                DiagnosticsScreen(entries, mon.target.top_tool or entries[0]["key"]),
                handle,
            )

        self._ensure_server(mon, choose)

    def _diag_entries(self) -> list[dict]:
        """Built-in diagnostics + the user's saved tools + the 'add a tool' action."""
        entries = list(DIAG_MENU)
        for tool in self.cfg.custom_tools:
            entries.append({"key": f"tool:{tool}", "label": f"{tool} — your tool", "tui": True})
        entries.append({
            "key": "add_tool",
            "label": "＋ Run a tool… — add one (installs on the server if missing)",
            "tui": True,
        })
        return entries

    def _add_custom_tool(self, mon: Monitor) -> None:
        def handle(result: tuple[str, bool] | None) -> None:
            if result is None:
                return
            tool, install = result
            if tool not in self.cfg.custom_tools:
                self.cfg.custom_tools.append(tool)
            mon.target.top_tool = f"tool:{tool}"
            self.cfg.targets = [m.target for m in self.monitors]
            save_config(self.cfg)  # the tool now shows up in the menu next time
            t = mon.target
            remote = self._custom_tool_command(tool, install)
            argv = build_ssh_argv(t.ssh_user, t.host, t.port, remote_cmd=remote)
            self._start_terminal("right", argv, mon=mon, retry=self.action_top)

        self.push_screen(CustomToolScreen(), handle)

    def _diag_command(self, key: str) -> str:
        """Remote shell command for a menu key (reports get paged through less)."""
        if key.startswith("tool:"):
            # a saved tool: run it, installing it first if it's not there
            return self._custom_tool_command(key[len("tool:"):], install=True)
        entry = next((e for e in DIAG_MENU if e["key"] == key), DIAG_MENU[0])
        if entry["tui"]:
            return entry["cmd"]
        return f"( {entry['cmd']} ) 2>&1 | less -R"

    @staticmethod
    def _custom_tool_command(tool: str, install: bool) -> str:
        """Run `tool`; when install=True, install it via the server's package manager first."""
        q = shlex.quote(tool)
        if not install:
            return f"exec {q}"
        return (
            f"T={q}; "
            'if ! command -v "$T" >/dev/null 2>&1; then '
            'echo "Installing $T ..."; '
            'if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y "$T"; '
            'elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y "$T"; '
            'elif command -v yum >/dev/null 2>&1; then sudo yum install -y "$T"; '
            'elif command -v apk >/dev/null 2>&1; then sudo apk add "$T"; '
            'elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm "$T"; '
            'elif command -v zypper >/dev/null 2>&1; then sudo zypper install -y "$T"; '
            "else echo 'No supported package manager found.'; sleep 3; fi; "
            'fi; '
            'command -v "$T" >/dev/null 2>&1 && exec "$T" || { echo "$T is not available."; sleep 3; }'
        )

    _TERM_HINT = "Ctrl-]  EXIT to pingmon   ·   Shift+←/→ switch panel   ·   q exits the tool   ·   type the SSH password if asked"

    def _start_terminal(
        self, side: str, argv: list[str], *,
        mon: Monitor | None = None, retry=None,
    ) -> None:
        if side == "left":
            term = self.query_one("#left-term", TerminalPane)
            self.query_one("#table", DataTable).styles.display = "none"
            hint = self.query_one("#left-term-hint", Static)
        else:
            term = self.query_one("#right-term", TerminalPane)
            self.query_one("#detail-scroll", VerticalScroll).styles.display = "none"
            hint = self.query_one("#right-term-hint", Static)
        hint.update(self._TERM_HINT)
        hint.styles.display = "block"
        self._session = {"side": side, "mon": mon, "retry": retry}
        self.notify("Press  Ctrl-]  to exit this console back to pingmon", timeout=6)
        self.run_worker(self._run_terminal(term, argv, side), name=f"term-{side}")

    async def _run_terminal(self, term: TerminalPane, argv: list[str], side: str) -> None:
        try:
            await term.start(argv)
        except Exception as exc:  # ssh missing, pty error, etc. — don't crash
            self._restore_panel(side)
            self._session = None
            try:
                term.styles.display = "none"
            except Exception:
                pass
            self.notify(f"Could not start session: {exc}", severity="error", timeout=6)

    def _restore_panel(self, side: str) -> None:
        if side == "left":
            self.query_one("#table", DataTable).styles.display = "block"
            self.query_one("#left-term-hint", Static).styles.display = "none"
            self.focused_panel = "left"
        else:
            self.query_one("#detail-scroll", VerticalScroll).styles.display = "block"
            self.query_one("#right-term-hint", Static).styles.display = "none"
            self.focused_panel = "right"
        self._update_focus()

    def action_detach(self) -> None:
        """Exit the console in the focused panel (or any live one) to the dashboard."""
        focused = "#left-term" if self.focused_panel == "left" else "#right-term"
        # prefer the focused panel's console, else fall back to whichever is live
        for sel in (focused, "#left-term", "#right-term"):
            term = self.query_one(sel, TerminalPane)
            if term.running:
                term.stop()
                self.notify("Left the console", timeout=1.5)
                return

    @on(TerminalPane.Exited)
    def _on_terminal_exit(self, event: TerminalPane.Exited) -> None:
        side = "left" if event.pane.id == "left-term" else "right"
        self._restore_panel(side)

        sess, self._session = self._session, None
        # ssh exits 255 on connection/auth failure (e.g. wrong SSH port). Reopen
        # the setup dialog, prefilled, so the port/user can be corrected — a saved
        # wrong port would otherwise be reconnected to silently on every Enter.
        if (
            sess is not None
            and event.exit_code == 255
            and sess.get("mon") is not None
            and sess["mon"].target.server
        ):
            mon = sess["mon"]
            retry = sess.get("retry")
            self.notify(
                f"SSH failed ({mon.target.ssh_user}@{mon.target.host}:{mon.target.port}) "
                "— check the user/port.",
                severity="warning", timeout=4,
            )

            def setup(result: tuple[str, int] | None) -> None:
                if result is None:
                    return
                user, port = result
                self._promote_to_server(mon, user, port)
                if retry is not None:
                    retry()

            self.push_screen(
                SshSetupScreen(
                    mon.target.host,
                    default_user=mon.target.ssh_user,
                    default_port=mon.target.port,
                ),
                setup,
            )

    # ---------- panel focus ----------

    def action_focus_left(self) -> None:
        self.focused_panel = "left"
        self._update_focus()

    def action_focus_right(self) -> None:
        self.focused_panel = "right"
        self._update_focus()

    def _update_focus(self) -> None:
        left = self.query_one("#left", Vertical)
        right = self.query_one("#detail", Vertical)
        left.set_class(self.focused_panel == "left", "-focused")
        right.set_class(self.focused_panel == "right", "-focused")
        try:
            if self.focused_panel == "left":
                term = self.query_one("#left-term", TerminalPane)
                (term if term.running else self.query_one("#table", DataTable)).focus()
            else:
                term = self.query_one("#right-term", TerminalPane)
                target = term if term.running else self.query_one("#detail-scroll", VerticalScroll)
                target.focus()
        except Exception:
            pass

    def action_cursor_up(self) -> None:
        if self.focused_panel == "right":
            self.query_one("#detail-scroll", VerticalScroll).scroll_up()
        else:
            self.query_one("#table", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        if self.focused_panel == "right":
            self.query_one("#detail-scroll", VerticalScroll).scroll_down()
        else:
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
