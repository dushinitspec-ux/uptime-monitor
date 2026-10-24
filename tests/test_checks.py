import asyncio
import errno
import shutil
import socket
from collections.abc import AsyncIterator

import pytest

from app.checks import NetworkChecker, describe_error, parse_ping, ping_command, tcp_connect
from app.devices import Device


def ascii_text(text: str, codepage: str) -> str:
    """What `ping()` sees: console bytes decoded as ASCII with replacement characters."""
    return text.encode(codepage).decode("ascii", errors="replace")


# Real output captured on Windows 11 with Russian locale (console code page 866).
RUSSIAN_WINDOWS_REPLY = ascii_text(
    "\r\nОбмен пакетами с 127.0.0.1 по с 32 байтами данных:\r\n"
    "Ответ от 127.0.0.1: число байт=32 время<1мс TTL=128\r\n",
    "cp866",
)
RUSSIAN_WINDOWS_SLOW_REPLY = ascii_text(
    "Ответ от 192.168.1.1: число байт=32 время=14мс TTL=64\r\n", "cp866"
)
RUSSIAN_WINDOWS_TIMEOUT = ascii_text(
    "Обмен пакетами с 192.0.2.1 по с 32 байтами данных:\r\n"
    "Превышен интервал ожидания для запроса.\r\n",
    "cp866",
)
ENGLISH_WINDOWS_REPLY = "Reply from 1.1.1.1: bytes=32 time=12ms TTL=57\r\n"
ENGLISH_WINDOWS_UNREACHABLE = "Reply from 192.168.1.14: Destination host unreachable.\r\n"
LINUX_REPLY = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=11.6 ms\n"


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, RUSSIAN_WINDOWS_REPLY, (True, 1.0)),
        (0, RUSSIAN_WINDOWS_SLOW_REPLY, (True, 14.0)),
        (1, RUSSIAN_WINDOWS_TIMEOUT, (False, None)),
        (0, ENGLISH_WINDOWS_REPLY, (True, 12.0)),
        (0, LINUX_REPLY, (True, 11.6)),
        # Windows can exit 0 when a router says "unreachable": no TTL, so not a reply.
        (0, ENGLISH_WINDOWS_UNREACHABLE, (False, None)),
        (1, "", (False, None)),
    ],
)
def test_parse_ping(returncode: int, output: str, expected: tuple[bool, float | None]) -> None:
    assert parse_ping(returncode, output) == expected


def test_ping_command_per_platform() -> None:
    assert ping_command("10.0.0.1", 1.0, "win32") == ["ping", "-n", "1", "-w", "1000", "10.0.0.1"]
    assert ping_command("10.0.0.1", 1.0, "linux") == ["ping", "-c", "1", "-W", "1", "10.0.0.1"]
    assert ping_command("10.0.0.1", 2.0, "darwin") == ["ping", "-c", "1", "-t", "2", "10.0.0.1"]


def test_errors_are_described_in_english() -> None:
    assert describe_error(ConnectionRefusedError(errno.ECONNREFUSED, "Удаленный компьютер...")) == (
        "connection refused"
    )
    assert describe_error(socket.gaierror(11001, "Этот хост неизвестен.")) == "host name not found"
    assert describe_error(OSError(errno.EHOSTUNREACH, "...")) == "host unreachable"


@pytest.fixture
async def open_port() -> AsyncIterator[int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_tcp_open_port(open_port: int) -> None:
    ok, latency, problem = await tcp_connect("127.0.0.1", open_port, timeout_s=2)
    assert ok
    assert latency is not None
    assert problem is None


async def test_tcp_closed_port() -> None:
    port = free_port()
    # Windows reports a refused connection only after ~2 s (it retries), so wait longer.
    ok, _, problem = await tcp_connect("127.0.0.1", port, timeout_s=5)
    assert not ok
    assert problem == f"tcp/{port}: connection refused"


@pytest.mark.skipif(shutil.which("ping") is None, reason="no ping command")
async def test_real_ping_to_localhost(open_port: int) -> None:
    checker = NetworkChecker(ping_timeout_s=1, tcp_timeout_s=2, max_parallel=5)
    result = await checker.check(Device(name="self", ip="127.0.0.1", ports=(open_port,)))
    assert result.ok, result.problems
