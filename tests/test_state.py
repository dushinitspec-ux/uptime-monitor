from datetime import timedelta

from app.state import DeviceState, Event, EventKind, Rules, Status, apply
from tests.conftest import T0

RULES = Rules(fail_after=2, recover_after=2)


def run(rounds: list[bool], rules: Rules = RULES) -> tuple[DeviceState, list[Event]]:
    """Feed one result per minute through the state machine."""
    state, events = DeviceState(), []
    for minute, ok in enumerate(rounds):
        state, event = apply(
            state, device="cam", ok=ok, at=T0 + timedelta(minutes=minute), rules=rules
        )
        if event:
            events.append(event)
    return state, events


def test_first_good_check_is_up_without_alert() -> None:
    state, events = run([True])
    assert state.status is Status.UP
    assert events == []


def test_one_lost_ping_is_not_an_outage() -> None:
    state, events = run([True, False, True, True])
    assert state.status is Status.UP
    assert events == []


def test_down_after_two_failed_rounds() -> None:
    state, events = run([True, False, False])
    assert state.status is Status.DOWN
    assert [e.kind for e in events] == [EventKind.DOWN]
    assert events[0].since == T0 + timedelta(minutes=1)  # the first failed check


def test_down_alert_is_sent_once() -> None:
    _, events = run([True] + [False] * 30)
    assert [e.kind for e in events] == [EventKind.DOWN]


def test_recovery_reports_downtime() -> None:
    _, events = run([True, False, False, False, True, True])
    up = events[-1]
    assert up.kind is EventKind.UP
    # Down from the first failure (minute 1) to the first good check (minute 4).
    assert up.downtime == timedelta(minutes=3)


def test_flapping_device_does_not_spam() -> None:
    # Down, then it alternates good/bad: one good round is not enough to be UP again.
    _, events = run([True, False, False, True, False, True, False, True, False])
    assert [e.kind for e in events] == [EventKind.DOWN]


def test_device_already_down_at_start_alerts() -> None:
    state, events = run([False, False])
    assert state.status is Status.DOWN
    assert [e.kind for e in events] == [EventKind.DOWN]


def test_single_round_thresholds() -> None:
    _, events = run([True, False, True], Rules(fail_after=1, recover_after=1))
    assert [e.kind for e in events] == [EventKind.DOWN, EventKind.UP]
    assert events[1].downtime == timedelta(minutes=1)
