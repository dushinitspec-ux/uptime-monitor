from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.checks import CheckResult
from app.db import Repository
from app.devices import Device

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


class ScriptedChecker:
    """Returns pre-planned results: plan[device] is a list of True/False per round."""

    def __init__(self, plan: dict[str, list[bool]], start: datetime = T0) -> None:
        self.plan = plan
        self.round = {name: 0 for name in plan}
        self.start = start

    async def check(self, device: Device) -> CheckResult:
        i = self.round[device.name]
        self.round[device.name] += 1
        ok = self.plan[device.name][i]
        at = self.start + timedelta(minutes=i)
        return CheckResult(
            device.name, ok, at, 2.0 if ok else None, () if ok else ("ping: no reply",)
        )


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
async def repo() -> AsyncIterator[Repository]:
    repository = await Repository.connect(":memory:")
    yield repository
    await repository.close()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()
