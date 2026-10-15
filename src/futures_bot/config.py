from typing import Literal

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
