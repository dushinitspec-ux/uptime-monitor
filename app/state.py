"""UP / DOWN decisions as a pure state machine.

One failed check is not an outage: a ping packet can get lost on busy Wi-Fi. A device
goes DOWN after `fail_after` failed rounds in a row and comes back UP after
`recover_after` good rounds in a row, so a flapping device does not flood the chat.

The outage starts at the first failed check and ends at the first good check of the
recovery streak; that is the downtime reported in the alert.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class Status(StrEnum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class EventKind(StrEnum):
    DOWN = "down"
    UP = "up"


@dataclass(frozen=True)
class Rules:
    fail_after: int = 2
    recover_after: int = 2


@dataclass(frozen=True)
class DeviceState:
    status: Status = Status.UNKNOWN
    failures: int = 0
    successes: int = 0
    first_failure_at: datetime | None = None
    first_success_at: datetime | None = None
    down_since: datetime | None = None


@dataclass(frozen=True)
class Event:
    kind: EventKind
    device: str
    at: datetime
    since: datetime  # when the outage started
    problems: tuple[str, ...] = ()

    @property
    def downtime(self) -> timedelta:
        return self.at - self.since


def apply(
    state: DeviceState,
    *,
    device: str,
    ok: bool,
    at: datetime,
    rules: Rules,
    problems: tuple[str, ...] = (),
) -> tuple[DeviceState, Event | None]:
    if ok:
        first_success = state.first_success_at or at
        state = replace(
            state,
            failures=0,
            first_failure_at=None,
            successes=state.successes + 1,
            first_success_at=first_success,
        )
        if state.status is Status.UNKNOWN:
            return replace(state, status=Status.UP), None
        if state.status is Status.DOWN and state.successes >= rules.recover_after:
            assert state.down_since is not None
            event = Event(EventKind.UP, device, first_success, state.down_since)
            return replace(state, status=Status.UP, down_since=None), event
        return state, None

    first_failure = state.first_failure_at or at
    state = replace(
        state,
        successes=0,
        first_success_at=None,
        failures=state.failures + 1,
        first_failure_at=first_failure,
    )
    if state.status is not Status.DOWN and state.failures >= rules.fail_after:
        event = Event(EventKind.DOWN, device, at, first_failure, problems)
        return replace(state, status=Status.DOWN, down_since=first_failure), event
    return state, None
