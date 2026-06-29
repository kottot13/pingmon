"""System helpers: async traceroute and OS desktop notifications."""

from __future__ import annotations

import asyncio
import re
import sys

_TIME_RE = re.compile(r"([\d.]+)\s*ms")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def parse_hop(line: str) -> dict | None:
    """Parse one traceroute line into {hop, host, times, loss}."""
    line = line.strip()
    m = re.match(r"^\s*(\d+)\s+(.*)$", line)
    if not m:
        return None
    hop = int(m.group(1))
    rest = m.group(2)

    times: list[float | None] = []
    # count probes: every "* " is a loss, every "N ms" is a sample
    for token in re.findall(r"\*|[\d.]+\s*ms", rest):
        if token == "*":
            times.append(None)
        else:
            tm = _TIME_RE.search(token)
            times.append(float(tm.group(1)) if tm else None)

    ip_match = _IP_RE.search(rest)
    host = ip_match.group(0) if ip_match else ("*" if rest.strip().startswith("*") else rest.split()[0])

    sent = len(times) or 1
    lost = sum(1 for t in times if t is None)
    loss = 100.0 * lost / sent
    return {"hop": hop, "host": host, "times": times, "loss": loss}


async def traceroute(host: str, max_hops: int = 20, queries: int = 3):
    """Async generator yielding parsed hops as traceroute emits them."""
    if sys.platform == "win32":
        cmd = ["tracert", "-d", "-h", str(max_hops), host]
    else:
        cmd = ["traceroute", "-n", "-q", str(queries), "-w", "1",
               "-m", str(max_hops), host]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield {"error": f"`{cmd[0]}` not found on this system"}
        return

    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            text = raw.decode(errors="replace").rstrip()
            hop = parse_hop(text)
            if hop:
                yield hop
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        await proc.wait()


def is_global_ip(value: str) -> bool:
    """True if `value` is a public, internet-routable IP address."""
    import ipaddress

    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def flag_emoji(iso2: str | None) -> str:
    """ISO-3166 alpha-2 code -> regional-indicator flag emoji ('NL' -> 🇳🇱)."""
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return "🏳"
    iso2 = iso2.upper()
    return chr(0x1F1E6 + ord(iso2[0]) - 65) + chr(0x1F1E6 + ord(iso2[1]) - 65)


def iso_from_flag(flag: str) -> str:
    """Inverse of `flag_emoji`: a regional-indicator flag -> ISO alpha-2 ('🇳🇱' -> 'NL').

    Returns '' when `flag` is not exactly a pair of regional-indicator symbols
    (e.g. the white-flag placeholder 🏳 or a plain string). Used to show a plain
    two-letter country code, since most terminals can't render flag emoji.
    """
    letters = [chr(ord(c) - 0x1F1E6 + 65) for c in flag if 0x1F1E6 <= ord(c) <= 0x1F1FF]
    return "".join(letters) if len(letters) == 2 else ""


_GEO_FIELDS = "status,country,countryCode,regionName,city,isp,as,query"


async def geo_lookup(host: str) -> dict | None:
    """Look a host/IP up via ip-api.com (free, no key); returns a geo dict.

    Keys: country, countryCode, regionName, city, isp, as (ASN + name), query
    (the resolved IP). Returns None on any failure. The blocking HTTP call runs
    in a thread so the UI event loop is never stalled.
    """
    def fetch() -> dict:
        import json
        import urllib.request

        url = f"http://ip-api.com/json/{host}?fields={_GEO_FIELDS}"
        with urllib.request.urlopen(url, timeout=4) as resp:
            return json.load(resp)

    try:
        data = await asyncio.to_thread(fetch)
    except Exception:
        return None
    if data.get("status") != "success":
        return None
    if not data.get("country") and not data.get("countryCode"):
        return None
    return data


async def desktop_notify(title: str, message: str) -> None:
    """Best-effort OS desktop notification; silently ignored if unavailable."""
    safe = message.replace('"', "'")
    safe_title = title.replace('"', "'")
    if sys.platform == "darwin":
        script = f'display notification "{safe}" with title "{safe_title}"'
        cmd = ["osascript", "-e", script]
    elif sys.platform.startswith("linux"):
        cmd = ["notify-send", safe_title, safe]
    else:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except (FileNotFoundError, OSError):
        pass
