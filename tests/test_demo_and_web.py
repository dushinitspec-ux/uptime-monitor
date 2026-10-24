from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.db import Repository
from app.demo import backfill
from app.devices import load_devices
from app.monitor import Monitor
from app.state import Rules
from app.web import create_app
from tests.conftest import T0, RecordingNotifier, ScriptedChecker

DEMO_DEVICES = load_devices(Path(__file__).resolve().parent.parent / "devices.demo.yaml")
NOW = T0 + timedelta(days=7)


async def test_backfill_creates_a_week_of_realistic_history(repo: Repository) -> None:
    states = await backfill(repo, DEMO_DEVICES, Rules(), now=NOW, seed=1)

    assert set(states) == {d.name for d in DEMO_DEVICES}
    week = await repo.counts_since(NOW - timedelta(days=7))
    for device in DEMO_DEVICES:
        counts = week[device.name]
        assert counts.total == 7 * 24 * 60
        assert 95 < 100 * counts.ok / counts.total < 100  # some outages, mostly up
    incidents = await repo.recent_incidents(limit=1000)
    assert incidents, "the demo should contain outages"
    assert all(i.ended_at is None or i.ended_at > i.started_at for i in incidents)


@pytest.fixture
async def client(repo: Repository) -> AsyncIterator[httpx.AsyncClient]:
    await backfill(repo, DEMO_DEVICES, Rules(), now=NOW, seed=1)
    monitor = Monitor(
        DEMO_DEVICES,
        ScriptedChecker({}),
        repo,
        RecordingNotifier(),
        Rules(),
        ZoneInfo("Europe/Istanbul"),
        retention=timedelta(days=8),
        clock=lambda: NOW,
    )
    await monitor.load()
    app = create_app(monitor, repo, ZoneInfo("Europe/Istanbul"), lambda: NOW, demo=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_status_page(client: httpx.AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "Network status" in html
    assert "htmx.org" in html
    for device in DEMO_DEVICES:
        assert device.name in html
    assert "Recent outages" in html
    assert "demo mode" in html


async def test_partial_is_only_the_status_block(client: httpx.AsyncClient) -> None:
    resp = await client.get("/partials/status")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith('<div id="status"')
    assert "<html" not in resp.text


async def test_api_status(client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/status")).json()
    assert len(data["devices"]) == len(DEMO_DEVICES)
    assert data["up"] + data["down"] <= len(DEMO_DEVICES)
    assert all(0 <= d["uptime_7d"] <= 100 for d in data["devices"])


async def test_healthz(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
