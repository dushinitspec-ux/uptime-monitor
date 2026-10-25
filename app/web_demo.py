"""The in-browser demo on the portfolio site (Python running in Pyodide).

Real code: the check round of app/monitor.py with the UP/DOWN state machine (app/state.py),
the alert texts, the demo devices from devices.demo.yaml (loaded by app/devices.py), a week
of history from app/demo.py, and the status page rows from app/view.py.
Swapped out: SQLite becomes an in-memory store with the same methods as app.db.Repository
(tests check that both give the same results), Telegram becomes a list of sent messages,
the network becomes a simulated one in which the visitor can unplug devices, and the wall
clock becomes simulated time that the page moves forward one round at a time.

Only browser-safe modules are imported (no aiogram, aiosqlite, FastAPI or Jinja2).
The functions at the bottom take and return JSON-friendly values.
"""

import random
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.checks import CheckResult
from app.db import PROBLEM_SEPARATOR, Counts, Incident, from_db, to_db
from app.demo import FakeChecker, backfill, outage_problems
from app.devices import Device, load_devices
from app.messages import format_duration
from app.monitor import Monitor
from app.state import DeviceState, Rules, Status
from app.view import build_view

START = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)  # simulated "now" when the demo starts
ROUND = timedelta(seconds=60)  # one check round per minute, as with CHECK_INTERVAL_S=60
HISTORY_DAYS = 7
RETENTION = timedelta(days=8)  # RETENTION_DAYS default
RECENT_CHECKS = 12  # how many of the latest results per device the page shows
DEVICES_FILE = Path(__file__).resolve().parent.parent / "devices.demo.yaml"


def _seconds(value: timedelta) -> int:
    return int(value.total_seconds())


def _db_time(value: datetime) -> datetime:
    """A timestamp as it comes back from app.db: UTC, whole seconds."""
    restored = from_db(to_db(value))
    assert restored is not None
    return restored


class _Series:
    """One device's check results in time order, with running totals for fast counting."""

    def __init__(self) -> None:
        self.ts: list[str] = []  # app.db timestamps: string order is time order
        self.ok_before = [0]  # ok_before[i] = good checks among the first i results

    def add(self, ts: str, ok: bool) -> None:
        i = bisect_right(self.ts, ts)
        self.ts.insert(i, ts)
        self.ok_before.insert(i + 1, self.ok_before[i] + ok)
        for j in range(i + 2, len(self.ok_before)):  # only when a result arrives late
            self.ok_before[j] += ok

    def counts(self, lo: int, hi: int) -> Counts:
        return Counts(self.ok_before[hi] - self.ok_before[lo], hi - lo)

    def drop_before(self, ts: str) -> int:
        i = bisect_left(self.ts, ts)
        base = self.ok_before[i]
        del self.ts[:i]
        self.ok_before = [value - base for value in self.ok_before[i:]]
        return i


class MemoryRepository:
    """app.db.Repository without SQLite: the same methods and results, kept in memory.

    Like the real one it relies on the caller to commit, but nothing is ever rolled back:
    Monitor and backfill() only write what they have finished computing.
    """

    def __init__(self) -> None:
        self._series: dict[str, _Series] = {}
        self._latest: dict[str, CheckResult] = {}
        self._incidents: list[Incident] = []
        self._states: dict[str, DeviceState] = {}

    async def close(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    # Writes

    async def add_results(self, results: list[CheckResult]) -> None:
        for r in results:
            self._series.setdefault(r.device, _Series()).add(to_db(r.at), r.ok)
            problems = PROBLEM_SEPARATOR.join(r.problems).split(PROBLEM_SEPARATOR)
            self._latest[r.device] = CheckResult(
                r.device, r.ok, _db_time(r.at), r.latency_ms, tuple(p for p in problems if p)
            )

    async def save_states(self, states: dict[str, DeviceState]) -> None:
        for device, s in states.items():
            self._states[device] = DeviceState(
                status=s.status,
                failures=s.failures,
                successes=s.successes,
                first_failure_at=s.first_failure_at and _db_time(s.first_failure_at),
                first_success_at=s.first_success_at and _db_time(s.first_success_at),
                down_since=s.down_since and _db_time(s.down_since),
            )

    async def add_incidents(self, incidents: list[Incident]) -> None:
        for i in incidents:
            ended = i.ended_at and _db_time(i.ended_at)
            self._incidents.append(Incident(i.device, _db_time(i.started_at), ended, i.problems))

    async def close_incident(self, device: str, ended_at: datetime) -> None:
        self._incidents = [
            Incident(i.device, i.started_at, _db_time(ended_at), i.problems)
            if i.device == device and i.ended_at is None
            else i
            for i in self._incidents
        ]

    async def purge_checks(self, before: datetime) -> int:
        deleted = 0
        for device, series in self._series.items():
            deleted += series.drop_before(to_db(before))
            if not series.ts:
                self._latest.pop(device, None)
        return deleted

    # Reads

    async def load_states(self) -> dict[str, DeviceState]:
        return dict(self._states)

    async def has_history(self) -> bool:
        return any(series.ts for series in self._series.values())

    async def latest_results(self) -> dict[str, CheckResult]:
        return dict(self._latest)

    async def counts_since(self, since: datetime) -> dict[str, Counts]:
        start = to_db(since)
        result = {}
        for device, series in self._series.items():
            i = bisect_left(series.ts, start)
            if i < len(series.ts):
                result[device] = series.counts(i, len(series.ts))
        return result

    async def hourly_counts_since(self, since: datetime) -> dict[str, dict[str, Counts]]:
        """{device: {"2026-10-24T09": Counts}} - one bucket per UTC hour."""
        start = to_db(since)
        result: dict[str, dict[str, Counts]] = {}
        for device, series in self._series.items():
            i = bisect_left(series.ts, start)
            while i < len(series.ts):
                hour = series.ts[i][:13]
                end = bisect_left(series.ts, hour + "~", i)  # "~" sorts after ":"
                result.setdefault(device, {})[hour] = series.counts(i, end)
                i = end
        return result

    async def recent_incidents(self, limit: int = 10) -> list[Incident]:
        return sorted(self._incidents, key=lambda i: i.started_at, reverse=True)[:limit]


class ChatNotifier:
    """Stands in for TelegramNotifier: keeps what would have been sent, with the time."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self.sent: list[tuple[datetime, str]] = []

    async def send(self, text: str) -> None:
        self.sent.append((self._clock(), text))


class DemoNetwork:
    """The checker of the demo. Devices answer like FakeChecker in app/demo.py, but never
    fail at random; a device fails its checks while the visitor keeps it unplugged."""

    def __init__(
        self, devices: list[Device], rng: random.Random, clock: Callable[[], datetime]
    ) -> None:
        self._answers = FakeChecker(devices, rng, failure_multiplier=0)
        self._clock = clock
        self.unplugged: set[str] = set()
        self.recent: dict[str, list[bool]] = {d.name: [] for d in devices}

    async def check(self, device: Device) -> CheckResult:
        at = self._clock()
        if device.name in self.unplugged:
            result = CheckResult(device.name, False, at, None, outage_problems(device))
        else:
            result = self._answers.simulate(device, at)
        self.recent[device.name] = [*self.recent[device.name], result.ok][-RECENT_CHECKS:]
        return result


class UptimeDemo:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.now = START
        self.devices = load_devices(DEVICES_FILE)
        self.rules = Rules()  # DOWN after 2 failed rounds, UP after 2 good ones
        self.repo = MemoryRepository()
        self.notifier = ChatNotifier(lambda: self.now)
        self.network = DemoNetwork(self.devices, random.Random(seed), lambda: self.now)
        self.monitor = Monitor(
            self.devices,
            self.network,
            self.repo,  # type: ignore[arg-type]  # same methods as app.db.Repository
            self.notifier,
            self.rules,
            UTC,  # type: ignore[arg-type]  # simulated time is shown in UTC
            retention=RETENTION,
            clock=lambda: self.now,
        )
        self._reported = 0

    async def setup(self) -> None:
        """What DEMO_MODE=true does on the first start: a week of generated history."""
        await backfill(
            self.repo,  # type: ignore[arg-type]
            self.devices,
            self.rules,
            START,
            days=HISTORY_DAYS,
            seed=self.seed,
        )
        await self.monitor.load()

    async def run_round(self) -> None:
        self.now += ROUND
        await self.monitor.run_round()
        await self.monitor.purge_old_checks()

    def set_plugged(self, name: str, plugged: bool) -> None:
        if name not in {d.name for d in self.devices}:
            raise ValueError(f"unknown device {name!r}")
        if plugged:
            self.network.unplugged.discard(name)
        else:
            self.network.unplugged.add(name)

    def new_messages(self) -> list[dict[str, str]]:
        sent = self.notifier.sent[self._reported :]
        self._reported = len(self.notifier.sent)
        return [{"text": text, "clock": f"{at:%H:%M}"} for at, text in sent]

    async def snapshot(self) -> dict[str, Any]:
        view = await build_view(self.monitor, self.repo, self.now, UTC)  # type: ignore[arg-type]
        rows = []
        for row in view["rows"]:
            state = self.monitor.states.get(row.name, DeviceState())
            down_since = state.down_since if row.status is Status.DOWN else None
            rows.append(
                {
                    "name": row.name,
                    "ip": row.ip,
                    "checks": row.checks,
                    "status": row.status.value,
                    "down_for": row.down_for,
                    "down_for_s": _seconds(self.now - down_since) if down_since else None,
                    "problems": row.problems,
                    "latency": row.latency,
                    "last_check": row.last_check,
                    "uptime_24h": row.uptime_24h_label,
                    "uptime_7d": row.uptime_7d_label,
                    "bars": [
                        {
                            "hour": bar.start.astimezone(UTC).strftime("%H:00"),
                            "level": bar.level,
                            "uptime": None if bar.uptime is None else round(bar.uptime, 1),
                        }
                        for bar in row.bars
                    ],
                    "failures": state.failures,
                    "successes": state.successes,
                    "recent": self.network.recent[row.name],
                    "unplugged": row.name in self.network.unplugged,
                }
            )
        return {
            "now": int(self.now.timestamp()),
            "time": f"{self.now:%H:%M}",
            "updated": view["updated"],
            "up": view["up"],
            "down": view["down"],
            "total": view["total"],
            "rows": rows,
            "incidents": [
                {
                    "device": i.device,
                    "started": f"{i.started_at:%d %b %H:%M}",
                    "started_ts": int(i.started_at.timestamp()),
                    "duration": format_duration(i.ended_at - i.started_at) if i.ended_at else None,
                    "duration_s": _seconds(i.ended_at - i.started_at) if i.ended_at else None,
                    "problems": i.problems,
                }
                for i in view["incidents"]
            ],
            "rules": {
                "fail_after": self.rules.fail_after,
                "recover_after": self.rules.recover_after,
                "round_s": int(ROUND.total_seconds()),
                "history_days": HISTORY_DAYS,
            },
            "messages": self.new_messages(),
        }


# The page's API. One demo per page; start() begins a new one (Replay calls it again).

_demo: UptimeDemo | None = None


def _current() -> UptimeDemo:
    if _demo is None:
        raise RuntimeError("call start() first")
    return _demo


async def start(seed: int = 5) -> dict[str, Any]:
    global _demo
    _demo = UptimeDemo(seed)
    await _demo.setup()
    return await _demo.snapshot()


async def step(rounds: int = 1) -> dict[str, Any]:
    demo = _current()
    for _ in range(max(1, min(int(rounds), 60))):
        await demo.run_round()
    return await demo.snapshot()


async def set_plugged(name: str, plugged: bool) -> dict[str, Any]:
    demo = _current()
    demo.set_plugged(str(name), bool(plugged))
    return await demo.snapshot()
