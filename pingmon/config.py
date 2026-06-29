"""Load/save target configuration. Format is TOML, meant to be hand-edited."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Built-in set: 1-2 reachable hosts per country from the VPS list + the US.
# All verified reachable over TCP. The port is configurable per target.
DEFAULT_TARGETS: list[dict] = [
    {"country": "Netherlands", "flag": "🇳🇱", "host": "speedtest.ams1.nl.leaseweb.net", "port": 80},
    {"country": "Netherlands", "flag": "🇳🇱", "host": "ams.speedtest.clouvider.net", "port": 80},
    {"country": "Germany", "flag": "🇩🇪", "host": "speedtest.frankfurt.linode.com", "port": 443},
    {"country": "Germany", "flag": "🇩🇪", "host": "ftp.fau.de", "port": 80},
    {"country": "United Kingdom", "flag": "🇬🇧", "host": "speedtest.london.linode.com", "port": 443},
    {"country": "United Kingdom", "flag": "🇬🇧", "host": "lon.speedtest.clouvider.net", "port": 443},
    {"country": "France", "flag": "🇫🇷", "host": "scaleway.testdebit.info", "port": 80},
    {"country": "Cyprus", "flag": "🇨🇾", "host": "ftp.cs.ucy.ac.cy", "port": 80},
    {"country": "Cyprus", "flag": "🇨🇾", "host": "mirror.library.ucy.ac.cy", "port": 80},
    {"country": "Italy", "flag": "🇮🇹", "host": "mirror.garr.it", "port": 80},
    {"country": "Italy", "flag": "🇮🇹", "host": "giano.com.dist.unige.it", "port": 80},
    {"country": "Spain", "flag": "🇪🇸", "host": "ftp.cica.es", "port": 80},
    {"country": "Greece", "flag": "🇬🇷", "host": "ftp.ntua.gr", "port": 80},
    {"country": "Greece", "flag": "🇬🇷", "host": "ftp.cc.uoc.gr", "port": 80},
    {"country": "Sweden", "flag": "🇸🇪", "host": "speedtest.tele2.net", "port": 80},
    {"country": "Ireland", "flag": "🇮🇪", "host": "ftp.heanet.ie", "port": 80},
    {"country": "United States", "flag": "🇺🇸", "host": "speedtest.newark.linode.com", "port": 443},
    {"country": "United States", "flag": "🇺🇸", "host": "la.speedtest.clouvider.net", "port": 443},
    {"country": "United States", "flag": "🇺🇸", "host": "mirror.us.leaseweb.net", "port": 80},
]

DEFAULTS = {
    "interval": 2.0,        # poll period per target, seconds
    "timeout": 2.0,         # TCP connect timeout, seconds
    "history": 90,          # number of samples kept in memory for the graph
    "alert_latency": 300.0, # alert if latency exceeds this (ms); 0 disables
    "alert_loss": 20.0,     # alert if loss over the window exceeds this (%); 0 disables
    "alert_window": 3,      # consecutive bad samples before an alert fires
    "desktop_notify": True, # also raise an OS desktop notification on alert
}


@dataclass
class Target:
    country: str
    flag: str
    host: str
    port: int = 443
    source: str = "builtin"  # "builtin" = shipped default, "user" = added/edited

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def code(self) -> str:
        """Two-letter display badge shown in place of the flag emoji.

        Derived from the flag (which encodes the ISO alpha-2 code), falling back
        to the first letters of the country name. Plain text renders reliably in
        every terminal, unlike flag emoji.
        """
        from .netutil import iso_from_flag

        iso = iso_from_flag(self.flag)
        if iso:
            return iso
        letters = "".join(ch for ch in self.country if ch.isalpha())
        return letters[:2].upper() or "??"


@dataclass
class Config:
    interval: float = DEFAULTS["interval"]
    timeout: float = DEFAULTS["timeout"]
    history: int = DEFAULTS["history"]
    alert_latency: float = DEFAULTS["alert_latency"]
    alert_loss: float = DEFAULTS["alert_loss"]
    alert_window: int = DEFAULTS["alert_window"]
    desktop_notify: bool = DEFAULTS["desktop_notify"]
    targets: list[Target] = field(default_factory=list)
    path: Path | None = None


def config_path() -> Path:
    """Resolve the config file location.

    Order: $PINGMON_CONFIG (explicit override) → $XDG_CONFIG_HOME/pingmon/config.toml
    (the default). The location is deliberately independent of the current working
    directory: targets you add are always saved to — and reloaded from — the same
    file, no matter where you launch pingmon from. (A previous build also picked up
    a ./config.toml in the launch directory, which silently lost added targets when
    you started pingmon from a different folder. Set $PINGMON_CONFIG for a custom
    location during development.)
    """
    env = os.environ.get("PINGMON_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg / "pingmon" / "config.toml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        cfg = Config(
            targets=[Target(**t) for t in DEFAULT_TARGETS],
            path=path,
        )
        save_config(cfg)
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    default_hosts = {t["host"] for t in DEFAULT_TARGETS}
    targets = [
        Target(
            country=t.get("country", "Unknown"),
            flag=t.get("flag", "🏳"),
            host=t["host"],
            port=int(t.get("port", 443)),
            # honour an explicit source; otherwise infer from the built-in set
            source=t.get("source") or ("builtin" if t["host"] in default_hosts else "user"),
        )
        for t in data.get("targets", [])
    ]
    if not targets:
        targets = [Target(**t) for t in DEFAULT_TARGETS]

    return Config(
        interval=float(data.get("interval", DEFAULTS["interval"])),
        timeout=float(data.get("timeout", DEFAULTS["timeout"])),
        history=int(data.get("history", DEFAULTS["history"])),
        alert_latency=float(data.get("alert_latency", DEFAULTS["alert_latency"])),
        alert_loss=float(data.get("alert_loss", DEFAULTS["alert_loss"])),
        alert_window=int(data.get("alert_window", DEFAULTS["alert_window"])),
        desktop_notify=bool(data.get("desktop_notify", DEFAULTS["desktop_notify"])),
        targets=targets,
        path=path,
    )


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def save_config(cfg: Config) -> None:
    """Minimal serialiser for our schema (no external dependencies)."""
    path = cfg.path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# pingmon configuration. Edit freely: add your own hosts/IPs.",
        "# interval — poll period per target (s), timeout — connect timeout (s).",
        "",
        f"interval = {cfg.interval}",
        f"timeout = {cfg.timeout}",
        f"history = {cfg.history}",
        "",
        "# Alerts: fire when a target stays bad for `alert_window` samples.",
        "# Set alert_latency or alert_loss to 0 to disable that trigger.",
        f"alert_latency = {cfg.alert_latency}",
        f"alert_loss = {cfg.alert_loss}",
        f"alert_window = {cfg.alert_window}",
        f"desktop_notify = {'true' if cfg.desktop_notify else 'false'}",
        "",
    ]
    for t in cfg.targets:
        lines.append("[[targets]]")
        lines.append(f'country = "{_toml_escape(t.country)}"')
        lines.append(f'flag = "{_toml_escape(t.flag)}"')
        lines.append(f'host = "{_toml_escape(t.host)}"')
        lines.append(f"port = {t.port}")
        lines.append(f'source = "{_toml_escape(t.source)}"')
        lines.append("")

    # Write atomically: a crash or full disk mid-write can't truncate the existing
    # config and lose the user's targets.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)
