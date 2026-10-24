"""The check loop: every interval, check all devices at once, record, alert on changes."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.checks import Checker, CheckResult
from app.db import Incident, Repository
from app.devices import Device
from app.messages import format_event
from app.notifier import Notifier
from app.state import DeviceState, Event, EventKind, Rules, apply

logger = logging.getLogger(__name__)

PURGE_EVERY = timedelta(hours=1)


def utcnow() -> datetime:
    return datetime.now(UTC)


def step(
    states: dict[str, DeviceState], results: list[CheckResult], rules: Rules
) -> tuple[dict[str, DeviceState], list[Event]]:
    """Apply one round of results to the device states. Pure: no I/O."""
    new_states = dict(states)
    events = []
    for result in results:
        new_states[result.device], event = apply(
            states.get(result.device, DeviceState()),
            device=result.device,
            ok=result.ok,
            at=result.at,
            rules=rules,
            problems=result.problems,
        )
        if event:
            events.append(event)
    return new_states, events


class Monitor:
    def __init__(
        self,
        devices: list[Device],
        checker: Checker,
        repo: Repository,
        notifier: Notifier,
        rules: Rules,
        tz: ZoneInfo,
        retention: timedelta,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.devices = devices
        self._by_name = {d.name: d for d in devices}
        self._checker = checker
        self._repo = repo
        self._notifier = notifier
        self._rules = rules
        self._tz = tz
        self._retention = retention
        self._clock = clock
        self.states: dict[str, DeviceState] = {}
        self._last_purge: datetime | None = None

    async def load(self) -> None:
        """Restore states, so a restart does not re-announce outages already reported."""
        self.states = await self._repo.load_states()

    async def run_round(self) -> list[Event]:
        results = await asyncio.gather(*(self._checker.check(d) for d in self.devices))
        return await self.process(list(results))

    async def process(self, results: list[CheckResult]) -> list[Event]:
        self.states, events = step(self.states, results, self._rules)
        await self._repo.add_results(results)
        await self._repo.save_states({r.device: self.states[r.device] for r in results})
        for event in events:
            if event.kind is EventKind.DOWN:
                incident = Incident(event.device, event.since, None, "; ".join(event.problems))
                await self._repo.add_incidents([incident])
            else:
                await self._repo.close_incident(event.device, event.at)
        await self._repo.commit()
        for event in events:
            await self._notify(event)
        return events

    async def _notify(self, event: Event) -> None:
        text = format_event(event, self._by_name.get(event.device), self._tz)
        logger.info("%s is %s", event.device, event.kind.value.upper())
        try:
            await self._notifier.send(text)
        except Exception:
            # Telegram being unreachable must not stop the monitoring itself.
            logger.exception("Could not send the %s alert for %s", event.kind, event.device)

    async def purge_old_checks(self) -> None:
        now = self._clock()
        if self._last_purge and now - self._last_purge < PURGE_EVERY:
            return
        self._last_purge = now
        deleted = await self._repo.purge_checks(now - self._retention)
        await self._repo.commit()
        if deleted:
            logger.info("Deleted %d check results older than %s", deleted, self._retention)

    async def run_forever(self, interval_s: float) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.run_round()
                await self.purge_old_checks()
            except Exception:
                # One broken round (disk full, odd network error) must not end monitoring.
                logger.exception("Check round failed")
            await asyncio.sleep(max(0.0, interval_s - (loop.time() - started)))
