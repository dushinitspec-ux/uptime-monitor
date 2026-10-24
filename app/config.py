"""Application settings, loaded from environment variables or a `.env` file."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: "ALERT_CHAT_ID=" in .env means "not set", not "parse '' as a number".
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    # Demo mode: fake devices that fail at random, 7 days of generated history.
    demo_mode: bool = False
    devices_file: Path | None = None

    # Telegram. Without a token, alerts are printed to the console.
    bot_token: str = ""
    alert_chat_id: int | None = None

    # Checks
    check_interval_s: float = Field(default=60, ge=5)
    demo_interval_s: float = Field(default=5, ge=1)
    fail_after: int = Field(default=2, ge=1, description="failed rounds before DOWN")
    recover_after: int = Field(default=2, ge=1, description="good rounds before UP")
    ping_timeout_s: float = 1.0
    tcp_timeout_s: float = 3.0
    max_parallel_checks: int = 50

    # Storage and web page
    database_path: Path = Path("data/uptime.db")
    retention_days: int = 8
    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = "Europe/Istanbul"

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"unknown timezone {value!r}, use a name like Europe/Istanbul"
            ) from None
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def effective_devices_file(self) -> Path:
        if self.devices_file:
            return self.devices_file
        return Path("devices.demo.yaml" if self.demo_mode else "devices.yaml")

    @property
    def interval_s(self) -> float:
        return self.demo_interval_s if self.demo_mode else self.check_interval_s


@lru_cache
def get_settings() -> Settings:
    return Settings()
