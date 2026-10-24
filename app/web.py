"""Status page (server-rendered HTML, refreshed by HTMX) and a JSON API."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import Repository
from app.messages import format_duration
from app.monitor import Monitor
from app.state import Status
from app.uptime import Bar, format_percent, hourly_bars, percent

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass(frozen=True)
class DeviceRow:
    name: str
    ip: str
    checks: str
    status: Status
    last_check: str
    latency: str
    problems: str
    uptime_24h: float | None
    uptime_7d: float | None
    bars: list[Bar]
    down_for: str

    @property
    def uptime_24h_label(self) -> str:
        return format_percent(self.uptime_24h)

    @property
    def uptime_7d_label(self) -> str:
        return format_percent(self.uptime_7d)


async def build_view(
    monitor: Monitor, repo: Repository, now: datetime, tz: ZoneInfo
) -> dict[str, Any]:
    latest = await repo.latest_results()
    day = await repo.counts_since(now - timedelta(hours=24))
    week = await repo.counts_since(now - timedelta(days=7))
    hourly = await repo.hourly_counts_since(now - timedelta(hours=24))

    rows = []
    for device in monitor.devices:
        state = monitor.states.get(device.name)
        last = latest.get(device.name)
        status = state.status if state else Status.UNKNOWN
        down_for = ""
        if status is Status.DOWN and state and state.down_since:
            down_for = format_duration(now - state.down_since)
        rows.append(
            DeviceRow(
                name=device.name,
                ip=device.ip,
                checks=device.checks_label,
                status=status,
                last_check=last.at.astimezone(tz).strftime("%H:%M:%S") if last else "—",
                latency=f"{last.latency_ms:g} ms" if last and last.latency_ms is not None else "—",
                problems="; ".join(last.problems) if last and not last.ok else "",
                uptime_24h=percent(day.get(device.name)),
                uptime_7d=percent(week.get(device.name)),
                bars=hourly_bars(now, hourly.get(device.name, {})),
                down_for=down_for,
            )
        )

    counts = {s: sum(r.status is s for r in rows) for s in Status}
    return {
        "rows": rows,
        "up": counts[Status.UP],
        "down": counts[Status.DOWN],
        "total": len(rows),
        "incidents": await repo.recent_incidents(8),
        "updated": now.astimezone(tz).strftime("%d %b %Y, %H:%M:%S"),
        "tz": tz,
        "format_duration": format_duration,
    }


def create_app(
    monitor: Monitor,
    repo: Repository,
    tz: ZoneInfo,
    clock: Callable[[], datetime],
    demo: bool = False,
) -> FastAPI:
    app = FastAPI(title="Uptime Monitor", version="0.1.0")

    async def view() -> dict[str, Any]:
        return {**await build_view(monitor, repo, clock(), tz), "demo": demo}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "index.html", await view())

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_status.html", await view())

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        data = await view()
        return {
            "updated": clock().isoformat(),
            "up": data["up"],
            "down": data["down"],
            "devices": [
                {
                    "name": r.name,
                    "ip": r.ip,
                    "status": r.status.value,
                    "uptime_24h": None if r.uptime_24h is None else round(r.uptime_24h, 3),
                    "uptime_7d": None if r.uptime_7d is None else round(r.uptime_7d, 3),
                    "problems": r.problems,
                }
                for r in data["rows"]
            ],
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
