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


async def traceroute(host: str, port: int | None = None, max_hops: int = 20, queries: int = 3):
    """Async generator yielding parsed hops as traceroute emits them.

    When ``port`` is given on macOS, probe with TCP SYN to that (known-open) port
    instead of the default UDP. TCP probes reach hosts whose network drops UDP or
    ICMP — exactly the case where you can SSH in but a plain traceroute shows only
    ``*``. macOS ``traceroute`` is setuid root, so this needs no sudo. On Linux,
    TCP/ICMP modes require root, so the portable UDP default is kept.
    """
    import os

    base = ["traceroute", "-n", "-q", str(queries), "-w", "2", "-m", str(max_hops)]
    if sys.platform == "win32":
        cmd = ["tracert", "-d", "-h", str(max_hops), host]
    elif sys.platform == "darwin" and port:
        # macOS traceroute is setuid root, so TCP probes work without sudo.
        cmd = base + ["-P", "TCP", "-p", str(port), host]
    elif sys.platform.startswith("linux") and port and os.geteuid() == 0:
        # On Linux TCP/ICMP probes need privilege; use them only when we're root.
        cmd = base + ["-T", "-p", str(port), host]
    else:
        # Portable default: unprivileged UDP (works as a normal user on Linux).
        cmd = base + [host]
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


def ssh_agent_has_keys() -> bool:
    """True if an ssh-agent is reachable and holds at least one identity.

    Used to decide whether an SSH login can be non-interactive. When this is
    False, ssh will fall back to its own password prompt inside the live pane.
    """
    import os
    import subprocess

    if not os.environ.get("SSH_AUTH_SOCK"):
        return False
    try:
        # `ssh-add -l` exits 0 with a key list, 1 when the agent has no keys,
        # 2 when it can't talk to an agent.
        return subprocess.run(
            ["ssh-add", "-l"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).returncode == 0
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def build_ssh_argv(
    user: str, host: str, port: int = 22, remote_cmd: str | None = None
) -> list[str]:
    """Argv for an interactive SSH session, optionally running a remote command.

    ``-t`` forces a remote pty so full-screen tools (htop/atop/top) and the login
    shell behave correctly. With no ``remote_cmd`` you get a login shell.
    """
    argv = [
        "ssh", "-t", "-o", "ConnectTimeout=10",
        "-p", str(port), f"{user}@{host}",
    ]
    if remote_cmd:
        argv.append(remote_cmd)
    return argv


# One-shot remote snapshot for the detail-panel load summary. Uses only tools
# present on essentially every Linux; emits tagged lines that _parse_load reads.
_LOAD_CMD = (
    "LANG=C; "
    "printf 'LOAD '; cut -d' ' -f1-3 /proc/loadavg 2>/dev/null | tr '\\n' ' '; nproc 2>/dev/null; "
    "printf 'DWAIT '; ps -eo stat= 2>/dev/null | grep -c '^D'; "
    "printf 'ROOTFS '; df -hP / 2>/dev/null | awk 'NR==2{print $5}'; "
    "printf 'MEM '; free -m 2>/dev/null | awk '/^Mem:/{print $3\" \"$2}'; "
    "echo CPUPROC; ps -eo pcpu=,comm= --sort=-pcpu 2>/dev/null | head -3; "
    "echo MEMPROC; ps -eo pmem=,comm= --sort=-pmem 2>/dev/null | head -3"
)


def _parse_load(text: str) -> dict | None:
    """Parse _LOAD_CMD output into {la, ncpu, dwait, rootfs, mem_used/total, cpu, mem}."""
    info: dict = {"cpu": [], "mem": []}
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("LOAD "):
            nums = line.split()[1:]
            try:
                info["la"] = [float(x) for x in nums[:3]]
            except ValueError:
                pass
            if len(nums) >= 4 and nums[3].isdigit():
                info["ncpu"] = int(nums[3])
        elif line.startswith("DWAIT "):
            p = line.split()
            if len(p) > 1 and p[1].isdigit():
                info["dwait"] = int(p[1])
        elif line.startswith("ROOTFS "):
            p = line.split(maxsplit=1)
            if len(p) > 1:
                info["rootfs"] = p[1].strip()
        elif line.startswith("MEM "):
            p = line.split()
            if len(p) >= 3 and p[1].isdigit() and p[2].isdigit():
                info["mem_used"], info["mem_total"] = int(p[1]), int(p[2])
        elif line == "CPUPROC":
            section = "cpu"
        elif line == "MEMPROC":
            section = "mem"
        elif section and line.strip():
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    info[section].append((float(parts[0]), parts[1].strip()))
                except ValueError:
                    pass
    return info if "la" in info else None


async def fetch_server_load(user: str, host: str, port: int = 22, timeout: float = 6.0) -> dict | None:
    """Snapshot a server's load over SSH (key/agent auth only), or None.

    Runs non-interactively (``BatchMode=yes``) so it never blocks on a password —
    the live summary only works when ssh-agent or a key is set up. Returns the
    parsed dict from :func:`_parse_load`, or None on any failure.
    """
    argv = [
        "ssh", "-T", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(timeout)}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(port), f"{user}@{host}", _LOAD_CMD,
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 4)
    except (asyncio.TimeoutError, OSError):
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return None
    if proc.returncode != 0:
        return None
    return _parse_load(out.decode(errors="replace"))


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
