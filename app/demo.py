"""Demo mode: fake devices that fail at random, plus a week of generated history.

Each device is a two-state Markov chain: while up, it fails with a small chance per
check; while down, it recovers with a 30 % chance per check. That gives realistic
outages of a few minutes, with cameras and Wi-Fi failing more often than the router.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.checks import CheckResult
from app.db import Incident, Repository
from app.devices import Device
from app.monitor import step
from app.state import DeviceState, EventKind, Rules

# Chance per check (per minute in the history) that an up device goes down.
FAILURE_RATES = {"camera": 0.004, "wi-fi": 0.003, "printer": 0.002}
DEFAULT_FAILURE_RATE = 0.0005
RECOVERY_RATE = 0.3


def failure_rate(device: Device) -> float:
    name = device.name.lower()
    return next((rate for key, rate in FAILURE_RATES.items() if key in name), DEFAULT_FAILURE_RATE)


def outage_problems(device: Device) -> tuple[str, ...]:
    """What the checks report for a device that is switched off or unplugged."""
    ping = ["ping: no reply"] if device.ping else []
    return tuple(ping + [f"tcp/{port}: timed out" for port in device.ports])


class FakeChecker:
    def __init__(
        self, devices: list[Device], rng: random.Random, failure_multiplier: float = 1.0
    ) -> None:
        self._rng = rng
        self._multiplier = failure_multiplier
        self._down = {d.name: False for d in devices}

    def simulate(self, device: Device, at: datetime) -> CheckResult:
        if self._down[device.name]:
            self._down[device.name] = self._rng.random() >= RECOVERY_RATE
        else:
            self._down[device.name] = self._rng.random() < failure_rate(device) * self._multiplier

        if self._down[device.name]:
            return CheckResult(device.name, False, at, None, outage_problems(device))
        latency = (
            self._rng.uniform(8, 40)
            if "wi-fi" in device.name.lower()
            else (self._rng.uniform(0.4, 6))
        )
        return CheckResult(device.name, True, at, round(latency, 1))

    async def check(self, device: Device) -> CheckResult:
        return self.simulate(device, datetime.now(UTC))


async def backfill(
    repo: Repository,
    devices: list[Device],
    rules: Rules,
    now: datetime,
    days: int = 7,
    step_s: int = 60,
    seed: int | None = None,
) -> dict[str, DeviceState]:
    """Generate `days` of check history in memory and store it in one transaction."""
    checker = FakeChecker(devices, random.Random(seed))
    states: dict[str, DeviceState] = {}
    results: list[CheckResult] = []
    open_incidents: dict[str, Incident] = {}
    closed: list[Incident] = []

    at = now - timedelta(days=days)
    while at < now:
        round_results = [checker.simulate(device, at) for device in devices]
        states, events = step(states, round_results, rules)
        for event in events:
            if event.kind is EventKind.DOWN:
                open_incidents[event.device] = Incident(
                    event.device, event.since, None, "; ".join(event.problems)
                )
            else:
                closed.append(replace(open_incidents.pop(event.device), ended_at=event.at))
        results.extend(round_results)
        at += timedelta(seconds=step_s)

    await repo.add_results(results)
    await repo.add_incidents([*closed, *open_incidents.values()])
    await repo.save_states(states)
    await repo.commit()
    return states
