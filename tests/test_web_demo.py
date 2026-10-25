"""The in-browser demo adapter (app/web_demo.py), run here under normal CPython."""

import json
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import web_demo
from app.checks import CheckResult
from app.config import Settings
from app.db import Repository
from app.demo import backfill
from app.devices import load_devices
from app.monitor import Monitor
from app.state import Rules
from app.view import build_view
from app.web_demo import ROUND, DemoNetwork, MemoryRepository
from tests.conftest import T0, RecordingNotifier, ScriptedChecker

PROJECT = Path(__file__).resolve().parent.parent
DEVICES = load_devices(PROJECT / "devices.demo.yaml")
CAMERA = "Lobby camera"
UP, DOWN = True, False


async def fill(repo: Repository | MemoryRepository) -> tuple[Monitor, RecordingNotifier]:
    """Two days of generated history, then live rounds with an outage, then a purge."""
    now = T0 + timedelta(days=2, minutes=30)
    await backfill(repo, DEVICES, Rules(), now, days=2, seed=4)  # type: ignore[arg-type]
    plan = {d.name: [UP] * 8 for d in DEVICES}
    plan[CAMERA] = [UP, DOWN, DOWN, DOWN, UP, UP, DOWN, UP]
    notifier = RecordingNotifier()
    monitor = Monitor(
        DEVICES,
        ScriptedChecker(plan, start=now),
        repo,  # type: ignore[arg-type]
        notifier,
        Rules(),
        ZoneInfo("UTC"),
        retention=timedelta(days=1),
        clock=lambda: now + timedelta(minutes=8),
    )
    await monitor.load()
    for _ in range(8):
        await monitor.run_round()
    await monitor.purge_old_checks()
    return monitor, notifier


async def test_memory_store_gives_the_same_results_as_sqlite(repo: Repository) -> None:
    memory = MemoryRepository()
    sqlite_monitor, sqlite_sent = await fill(repo)
    memory_monitor, memory_sent = await fill(memory)
    assert sqlite_monitor.states == memory_monitor.states
    assert sqlite_sent.messages == memory_sent.messages
    assert len(memory_sent.messages) == 2  # the camera: DOWN, then back UP

    now = T0 + timedelta(days=2, minutes=38)
    for since in [T0, now - timedelta(hours=24), now - timedelta(minutes=95), now]:
        assert await repo.counts_since(since) == await memory.counts_since(since)
        assert await repo.hourly_counts_since(since) == await memory.hourly_counts_since(since)
    assert await repo.latest_results() == await memory.latest_results()
    assert await repo.load_states() == await memory.load_states()
    assert await repo.has_history() == await memory.has_history()
    sqlite_incidents = await repo.recent_incidents(1000)
    assert sorted(sqlite_incidents, key=repr) == sorted(
        await memory.recent_incidents(1000), key=repr
    )
    utc = ZoneInfo("UTC")
    assert await build_view(sqlite_monitor, repo, now, utc) == await build_view(
        memory_monitor,
        memory,  # type: ignore[arg-type]
        now,
        utc,
    )


async def test_memory_store_counts_results_that_arrive_late(repo: Repository) -> None:
    memory = MemoryRepository()
    at = [T0 + timedelta(minutes=m) for m in range(4)]
    batches = [
        [CheckResult("a", UP, at[0]), CheckResult("a", DOWN, at[2]), CheckResult("a", UP, at[3])],
        [CheckResult("a", DOWN, at[1], None, ("ping: no reply",))],  # older than the rest
    ]
    for store in (repo, memory):
        for batch in batches:
            await store.add_results(batch)
    for since in at:
        assert await memory.counts_since(since) == await repo.counts_since(since)
    assert (await memory.counts_since(T0))["a"] == (await repo.counts_since(T0))["a"]
    assert await memory.latest_results() == await repo.latest_results()
    assert await memory.purge_checks(at[2]) == await repo.purge_checks(at[2]) == 2
    assert await memory.counts_since(T0) == await repo.counts_since(T0)


def test_demo_rules_match_the_real_defaults() -> None:
    real = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (Rules().fail_after, Rules().recover_after) == (real.fail_after, real.recover_after)
    assert ROUND.total_seconds() == real.check_interval_s
    assert timedelta(days=real.retention_days) == web_demo.RETENTION


async def test_demo_network_fails_only_unplugged_devices() -> None:
    network = DemoNetwork(DEVICES, random.Random(1), lambda: T0)
    network.unplugged.add(CAMERA)
    results = {d.name: await network.check(d) for d in DEVICES}
    assert results[CAMERA].problems == ("ping: no reply", "tcp/554: timed out")
    assert [name for name, r in results.items() if not r.ok] == [CAMERA]
    printer = results["Reception printer"]
    assert printer.ok and printer.latency_ms is not None


async def test_demo_scenario_one_lost_round_then_an_outage() -> None:
    start = await web_demo.start(5)
    assert (start["up"], start["down"], start["total"]) == (8, 0, 8)
    assert start["time"] == "09:30"
    assert all(len(row["bars"]) == 24 for row in start["rows"])

    async def round_for_camera(plugged: bool) -> dict:
        await web_demo.set_plugged(CAMERA, plugged)
        snapshot = await web_demo.step()
        return next(r for r in snapshot["rows"] if r["name"] == CAMERA) | {
            "messages": snapshot["messages"]
        }

    one_miss = await round_for_camera(False)
    assert (one_miss["status"], one_miss["failures"], one_miss["messages"]) == ("up", 1, [])
    assert (await round_for_camera(True))["failures"] == 0

    await round_for_camera(False)
    down = await round_for_camera(False)
    assert (down["status"], down["down_for_s"]) == ("down", 60)
    assert [m["text"] for m in down["messages"]] == [
        "🔴 Lobby camera is DOWN (192.0.2.20)\n"
        "ping: no reply; tcp/554: timed out\n"
        "Not responding since 09:33."
    ]
    await round_for_camera(False)
    assert (await round_for_camera(True))["messages"] == []  # one good round is not enough
    back = await round_for_camera(True)
    assert back["status"] == "up"
    assert [m["text"] for m in back["messages"]] == [
        "🟢 Lobby camera is back UP (192.0.2.20)\nDowntime: 3 min (09:33 - 09:36)."
    ]
    assert back["recent"][-7:] == [False, True, False, False, False, True, True]

    final = await web_demo.step()
    assert final["incidents"][0] | {"started_ts": None} == {
        "device": CAMERA,
        "started": "24 Oct 09:33",
        "started_ts": None,
        "duration": "3 min",
        "duration_s": 180,
        "problems": "ping: no reply; tcp/554: timed out",
    }


def test_demo_imports_only_browser_safe_modules() -> None:
    code = (
        "import sys, app.web_demo; "
        "bad = ('aiogram', 'aiosqlite', 'fastapi', 'uvicorn', 'httpx', 'jinja2', "
        "'pydantic_settings', 'sqlite3'); "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_web_demo_json_lists_every_file_the_demo_uses() -> None:
    code = (
        "import json, sys, app.web_demo; "
        "print(json.dumps(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, check=True
    )
    modules = {
        "app/__init__.py" if m == "app" else m.replace(".", "/") + ".py"
        for m in json.loads(result.stdout)
    }
    config = json.loads((PROJECT / "web_demo.json").read_text(encoding="utf-8"))
    assert set(config["files"]) == modules | {"devices.demo.yaml"}
