from datetime import timedelta
from zoneinfo import ZoneInfo

from app.db import Repository
from app.devices import Device
from app.monitor import Monitor
from app.state import EventKind, Rules, Status
from tests.conftest import T0, RecordingNotifier, ScriptedChecker

CAMERA = Device(name="Lobby camera", ip="192.0.2.20", ports=(554,))
ROUTER = Device(name="Router", ip="192.0.2.1")
UP, DOWN = True, False


def make_monitor(
    repo: Repository, notifier: RecordingNotifier, plan: dict[str, list[bool]]
) -> Monitor:
    devices = [d for d in (CAMERA, ROUTER) if d.name in plan]
    return Monitor(
        devices,
        ScriptedChecker(plan),
        repo,
        notifier,
        Rules(2, 2),
        ZoneInfo("UTC"),
        retention=timedelta(days=8),
        clock=lambda: T0 + timedelta(days=30),
    )


async def run_rounds(monitor: Monitor, n: int) -> list[EventKind]:
    kinds = []
    for _ in range(n):
        kinds += [e.kind for e in await monitor.run_round()]
    return kinds


async def test_outage_alerts_down_then_up(repo: Repository, notifier: RecordingNotifier) -> None:
    plan = {CAMERA.name: [UP, DOWN, DOWN, DOWN, UP, UP], ROUTER.name: [UP] * 6}
    monitor = make_monitor(repo, notifier, plan)

    assert await run_rounds(monitor, 6) == [EventKind.DOWN, EventKind.UP]
    assert notifier.messages[0].startswith("🔴 Lobby camera is DOWN (192.0.2.20)")
    assert "ping: no reply" in notifier.messages[0]
    assert "Downtime: 3 min" in notifier.messages[1]

    incidents = await repo.recent_incidents()
    assert len(incidents) == 1
    assert incidents[0].ended_at == T0 + timedelta(minutes=4)


async def test_restart_does_not_repeat_the_alert(
    repo: Repository, notifier: RecordingNotifier
) -> None:
    plan = {CAMERA.name: [DOWN] * 6}
    first = make_monitor(repo, notifier, plan)
    await run_rounds(first, 3)
    assert len(notifier.messages) == 1

    second = make_monitor(repo, notifier, plan)  # the service restarted
    await second.load()
    second._checker = first._checker  # continue the same scripted rounds
    await run_rounds(second, 3)
    assert len(notifier.messages) == 1
    assert second.states[CAMERA.name].status is Status.DOWN


async def test_broken_telegram_does_not_stop_monitoring(repo: Repository) -> None:
    class BrokenNotifier(RecordingNotifier):
        async def send(self, text: str) -> None:
            raise ConnectionError("Telegram is unreachable")

    monitor = make_monitor(repo, BrokenNotifier(), {ROUTER.name: [DOWN, DOWN, DOWN]})
    assert await run_rounds(monitor, 3) == [EventKind.DOWN]
    assert monitor.states[ROUTER.name].status is Status.DOWN


async def test_results_are_stored_for_uptime(repo: Repository, notifier: RecordingNotifier) -> None:
    monitor = make_monitor(repo, notifier, {ROUTER.name: [UP, UP, DOWN, UP]})
    await run_rounds(monitor, 4)
    counts = await repo.counts_since(T0 - timedelta(hours=1))
    assert (counts[ROUTER.name].ok, counts[ROUTER.name].total) == (3, 4)


async def test_old_results_are_purged(repo: Repository, notifier: RecordingNotifier) -> None:
    monitor = make_monitor(repo, notifier, {ROUTER.name: [UP, UP]})
    await run_rounds(monitor, 2)
    await monitor.purge_old_checks()  # "now" is 30 days later, retention is 8 days
    assert not await repo.has_history()
