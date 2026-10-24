"""Uptime percentages and the 24 hourly bars shown on the status page."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db import Counts


def percent(counts: Counts | None) -> float | None:
    if counts is None or counts.total == 0:
        return None
    return 100 * counts.ok / counts.total


def format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 100:
        return "100%"
    # Never round 99.96 up to "100%": that would hide an outage.
    return f"{int(value * 100) / 100:.2f}%"


@dataclass(frozen=True)
class Bar:
    start: datetime
    uptime: float | None  # None = no checks in that hour

    @property
    def level(self) -> str:
        if self.uptime is None:
            return "none"
        if self.uptime == 100:
            return "up"
        return "partial" if self.uptime >= 90 else "down"


def hourly_bars(now: datetime, buckets: dict[str, Counts], hours: int = 24) -> list[Bar]:
    """One bar per hour, oldest first, the last one being the current hour."""
    current = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    bars = []
    for back in range(hours - 1, -1, -1):
        start = current - timedelta(hours=back)
        key = start.strftime("%Y-%m-%dT%H")
        bars.append(Bar(start, percent(buckets.get(key))))
    return bars
