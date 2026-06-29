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
