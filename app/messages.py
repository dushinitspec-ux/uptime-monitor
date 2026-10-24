"""Alert texts (plain text, no Markdown)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.devices import Device
from app.state import Event, EventKind


def format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _clock(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%H:%M")


def format_event(event: Event, device: Device | None, tz: ZoneInfo) -> str:
    address = f" ({device.ip})" if device else ""
    if event.kind is EventKind.DOWN:
        lines = [f"🔴 {event.device} is DOWN{address}"]
        if event.problems:
            lines.append("; ".join(event.problems))
        lines.append(f"Not responding since {_clock(event.since, tz)}.")
        return "\n".join(lines)
    return (
        f"🟢 {event.device} is back UP{address}\n"
        f"Downtime: {format_duration(event.downtime)} "
        f"({_clock(event.since, tz)} - {_clock(event.at, tz)})."
    )
