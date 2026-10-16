from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BOT_",
        extra="ignore",
    )

    mode: Literal["backtest", "paper"] = "paper"
    exchange_environment: Literal["testnet", "production"] = "testnet"
    database_url: str = "sqlite:///./data/futures_platform.sqlite3"
    reporting_timezone: str = "America/Bogota"
    dashboard_enabled: bool = False
    public_origin: str = "https://binancebot.albotero.com"
    secure_cookies: bool = True
    frontend_directory: str = "frontend/dist"
    session_lifetime_seconds: int = Field(default=28800, ge=300, le=86400)

    @field_validator("reporting_timezone")
    @classmethod
    def validate_reporting_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown reporting timezone: {value}") from exc
        return value
