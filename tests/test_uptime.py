from datetime import UTC, datetime, timedelta

from app.db import Counts
from app.messages import format_duration
from app.uptime import format_percent, hourly_bars, percent


def test_percent() -> None:
    assert percent(Counts(ok=99, total=100)) == 99
    assert percent(Counts(ok=0, total=0)) is None
    assert percent(None) is None


def test_format_percent_never_rounds_up_to_100() -> None:
    assert format_percent(100) == "100%"
    assert format_percent(99.999) == "99.99%"  # an outage must stay visible
    assert format_percent(98.756) == "98.75%"
    assert format_percent(None) == "—"


def test_hourly_bars() -> None:
    now = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
    buckets = {"2026-10-24T09": Counts(30, 30), "2026-10-24T08": Counts(57, 60)}
    bars = hourly_bars(now, buckets)
    assert len(bars) == 24
    assert bars[-1].start == datetime(2026, 10, 24, 9, tzinfo=UTC)
    assert bars[-1].level == "up"
    assert bars[-2].level == "partial"  # 95 %
    assert bars[0].start == bars[-1].start - timedelta(hours=23)
    assert bars[0].level == "none"


def test_format_duration() -> None:
    assert format_duration(timedelta(seconds=24)) == "24 s"
    assert format_duration(timedelta(minutes=12, seconds=30)) == "12 min 30 s"
    assert format_duration(timedelta(hours=2, minutes=5)) == "2 h 5 min"
    assert format_duration(timedelta(days=1, hours=3)) == "1 d 3 h"
