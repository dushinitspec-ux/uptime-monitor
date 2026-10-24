"""Real network checks: ICMP ping (system `ping` command) and TCP connect."""

import asyncio
import contextlib
import errno
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.devices import Device

# The output is decoded as ASCII (see `ping`), so only Latin text survives. Linux and
# English Windows print "time=12.3 ms". Localized Windows loses its words ("время<1мс"
# becomes unreadable), but the number always comes right before " TTL=".
_LATENCY_LATIN = re.compile(r"time[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
_LATENCY_BEFORE_TTL = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\S*\s+TTL=")


@dataclass(frozen=True)
class CheckResult:
    device: str
    ok: bool
    at: datetime
    latency_ms: float | None = None
    problems: tuple[str, ...] = ()


class Checker(Protocol):
    async def check(self, device: Device) -> CheckResult: ...


def ping_command(host: str, timeout_s: float, platform: str = sys.platform) -> list[str]:
    if platform == "win32":
        return ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
    if platform == "darwin":
        return ["ping", "-c", "1", "-t", str(max(1, round(timeout_s))), host]
    return ["ping", "-c", "1", "-W", str(max(1, round(timeout_s))), host]


def parse_ping(returncode: int, output: str) -> tuple[bool, float | None]:
    """(reachable, round-trip ms) from the ping command's exit code and output.

    Windows ping can exit with 0 when a router answers "Destination host unreachable",
    so a reply line with a TTL is required as well. "TTL=" is printed in every locale.
    """
    ok = returncode == 0 and "ttl=" in output.lower()
    if not ok:
        return False, None
    match = _LATENCY_LATIN.search(output) or _LATENCY_BEFORE_TTL.search(output)
    latency = float(match.group(1).replace(",", ".")) if match else None
    return ok, latency


def describe_error(exc: OSError) -> str:
    """A short English reason. Windows `strerror` is localized ("Удаленный компьютер
    отклонил..."), which would end up in an English status page and alerts."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, socket.gaierror):
        return "host name not found"
    if exc.errno in (errno.EHOSTUNREACH, getattr(errno, "WSAEHOSTUNREACH", None)):
        return "host unreachable"
    if exc.errno in (errno.ENETUNREACH, getattr(errno, "WSAENETUNREACH", None)):
        return "network unreachable"
    return f"error {exc.errno}" if exc.errno else exc.__class__.__name__


async def ping(host: str, timeout_s: float) -> tuple[bool, float | None, str | None]:
    proc = await asyncio.create_subprocess_exec(
        *ping_command(host, timeout_s),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout_s + 5)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, None, "ping: timed out"
    # Only ASCII parts ("TTL=", digits) matter, so the console code page does not.
    ok, latency = parse_ping(proc.returncode or 0, output.decode("ascii", errors="replace"))
    return ok, latency, None if ok else "ping: no reply"


async def tcp_connect(
    host: str, port: int, timeout_s: float
) -> tuple[bool, float | None, str | None]:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            _, writer = await asyncio.open_connection(host, port)
    except TimeoutError:
        return False, None, f"tcp/{port}: timed out"
    except OSError as exc:
        return False, None, f"tcp/{port}: {describe_error(exc)}"
    latency = (time.perf_counter() - started) * 1000
    writer.close()
    # The device may reset the connection on close; it answered, which is what we checked.
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True, latency, None


class NetworkChecker:
    def __init__(self, ping_timeout_s: float, tcp_timeout_s: float, max_parallel: int) -> None:
        self._ping_timeout = ping_timeout_s
        self._tcp_timeout = tcp_timeout_s
        self._limit = asyncio.Semaphore(max_parallel)

    async def check(self, device: Device) -> CheckResult:
        async with self._limit:
            probes = [ping(device.ip, self._ping_timeout)] if device.ping else []
            probes += [tcp_connect(device.ip, p, self._tcp_timeout) for p in device.ports]
            outcomes = await asyncio.gather(*probes)
        latencies = [lat for ok, lat, _ in outcomes if ok and lat is not None]
        return CheckResult(
            device=device.name,
            ok=all(ok for ok, _, _ in outcomes),
            at=datetime.now(UTC),
            latency_ms=round(latencies[0], 1) if latencies else None,
            problems=tuple(problem for _, _, problem in outcomes if problem),
        )
