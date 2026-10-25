"""Status page (server-rendered HTML, refreshed by HTMX) and a JSON API."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import Repository
from app.monitor import Monitor
from app.view import build_view

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


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
