"""Where alerts go: a Telegram chat, or the console when no bot is configured."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

# Type hint only: the Notifier protocol is also used by the in-browser demo, which has no
# Telegram library.
if TYPE_CHECKING:
    from aiogram import Bot


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class ConsoleNotifier:
    async def send(self, text: str) -> None:
        print(f"\n----- alert -----\n{text}\n-----------------", flush=True)


class TelegramNotifier:
    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        await self._bot.send_message(self._chat_id, text)
