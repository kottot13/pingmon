"""Asynchronous TCP ping: measure connect time to host:port."""

from __future__ import annotations

import asyncio


async def tcp_ping(host: str, port: int, timeout: float = 2.0) -> float | None:
    """Return latency in milliseconds, or None if unreachable.

    Measures the full TCP connect round-trip (SYN -> SYN/ACK), which is
    close to the real network latency to the service on the given port.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    writer = None
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        elapsed = (loop.time() - start) * 1000.0
        return elapsed
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        if writer is not None:
            try:
                writer.close()
                # don't await wait_closed — close timing is irrelevant here
            except Exception:
                pass


async def tcp_ping_banner(host: str, port: int, timeout: float = 2.0) -> float | None:
    """TCP-connect latency, but only counted as UP if the server sends a banner.

    Unlike :func:`tcp_ping`, this does not trust a bare TCP handshake. After the
    connection opens it waits for the server to *say something* (an SSH server
    sends its ``SSH-2.0-...`` banner immediately). A host that only completes the
    handshake — e.g. a box behind a DDoS scrubber that answers SYN/ACK at the
    edge while the real daemon is dead — yields None, so it reads as DOWN instead
    of a false UP. Only meaningful for banner-sending services (SSH, FTP, SMTP).

    The returned latency is the *connect* round-trip (true network RTT), not the
    time until the banner: some sshd setups delay the banner (reverse DNS,
    GSSAPI), which would otherwise inflate the number far above the real RTT.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        connect_ms = (loop.time() - start) * 1000.0
        remaining = timeout - (loop.time() - start)
        if remaining <= 0:
            return None
        data = await asyncio.wait_for(reader.read(1), timeout=remaining)
        if not data:  # connection opened then closed without a banner
            return None
        return connect_ms
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def resolve(host: str) -> str | None:
    """Resolve a host to an IP (for display in the detail panel)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None), timeout=3.0
        )
        if infos:
            return infos[0][4][0]
    except (OSError, asyncio.TimeoutError):
        return None
    return None
