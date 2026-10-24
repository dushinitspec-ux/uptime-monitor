"""Entry point: runs the check loop and the status page in one process."""

import asyncio
import logging
import random
import shutil
import sys
from datetime import timedelta

import uvicorn
from aiogram import Bot

from app.checks import Checker, NetworkChecker
from app.config import Settings, get_settings
from app.db import Repository
from app.demo import FakeChecker, backfill
from app.devices import DeviceConfigError, load_devices
from app.monitor import Monitor, utcnow
from app.notifier import ConsoleNotifier, Notifier, TelegramNotifier
from app.state import Rules
from app.web import create_app

logger = logging.getLogger("uptime")

# Live demo devices fail more often than in the generated history, so alerts show up
# within a minute or two instead of once a day.
DEMO_LIVE_FAILURE_MULTIPLIER = 8


async def run(settings: Settings) -> None:
    devices = load_devices(settings.effective_devices_file)
    rules = Rules(settings.fail_after, settings.recover_after)
    repo = await Repository.connect(settings.database_path)

    bot: Bot | None = None
    notifier: Notifier = ConsoleNotifier()
    if settings.bot_token and settings.alert_chat_id is not None:
        bot = Bot(settings.bot_token)
        notifier = TelegramNotifier(bot, settings.alert_chat_id)
    else:
        logger.warning("BOT_TOKEN or ALERT_CHAT_ID is not set: alerts are printed to the console.")

    checker: Checker
    if settings.demo_mode:
        if not await repo.has_history():
            logger.info("Demo mode: generating 7 days of history for %d devices...", len(devices))
            await backfill(repo, devices, rules, utcnow())
        checker = FakeChecker(devices, random.Random(), DEMO_LIVE_FAILURE_MULTIPLIER)
    else:
        if any(d.ping for d in devices) and shutil.which("ping") is None:
            logger.error("The 'ping' command is not installed, so every ping check will fail.")
        checker = NetworkChecker(
            settings.ping_timeout_s, settings.tcp_timeout_s, settings.max_parallel_checks
        )

    monitor = Monitor(
        devices,
        checker,
        repo,
        notifier,
        rules,
        settings.tz,
        retention=timedelta(days=settings.retention_days),
    )
    await monitor.load()
    app = create_app(monitor, repo, settings.tz, utcnow, demo=settings.demo_mode)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port))

    logger.info(
        "Watching %d devices every %g s. Status page: http://127.0.0.1:%d",
        len(devices),
        settings.interval_s,
        settings.port,
    )
    checks = asyncio.create_task(monitor.run_forever(settings.interval_s))
    try:
        await server.serve()
    finally:
        checks.cancel()
        await asyncio.gather(checks, return_exceptions=True)
        if bot:
            await bot.session.close()
        await repo.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    try:
        asyncio.run(run(settings))
    except DeviceConfigError as exc:
        sys.exit(f"Device list error: {exc}")


if __name__ == "__main__":
    main()
