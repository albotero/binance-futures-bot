from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
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

    @field_validator("reporting_timezone")
    @classmethod
    def validate_reporting_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown reporting timezone: {value}") from exc
        return value
