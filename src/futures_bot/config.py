from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import BotConfig, StrategyProfile, StrategyRule


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_path(raw: str | Path, base: Path | None = None) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (base or project_root()).joinpath(path).resolve()


def config_dir(config: BotConfig | None = None) -> Path:
    base = resolve_path((config.data_dir if config else "data"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def load_bot_config() -> BotConfig:
    load_dotenv()
    config = BotConfig()
    config.mode = os.getenv("BOT_MODE", config.mode)
    config.symbols = [symbol.strip().upper() for symbol in os.getenv(
        "BOT_SYMBOLS", ",".join(config.symbols)).split(",") if symbol.strip()]
    config.trade_all_symbols = _read_bool(
        "BOT_TRADE_ALL_SYMBOLS", config.trade_all_symbols)
    config.allow_short = _read_bool("BOT_ALLOW_SHORT", config.allow_short)
    config.live_protection_mode = os.getenv(
        "BOT_LIVE_PROTECTION_MODE", config.live_protection_mode).strip().lower() or config.live_protection_mode
    config.backtest_duration = os.getenv(
        "BOT_BACKTEST_DURATION", config.backtest_duration).strip() or config.backtest_duration
    config.candle_style = os.getenv("BOT_CANDLE_STYLE", config.candle_style)
    config.interval = os.getenv("BOT_INTERVAL", config.interval)
    config.candles_limit = _read_int("BOT_CANDLES_LIMIT", config.candles_limit)
    config.leverage = _read_int("BOT_LEVERAGE", config.leverage)
    config.max_leverage = _read_int("BOT_MAX_LEVERAGE", config.max_leverage)
    config.risk_per_trade_pct = _read_float(
        "BOT_RISK_PER_TRADE_PCT", config.risk_per_trade_pct)
    config.risk_reward_ratio = _read_float(
        "BOT_RISK_REWARD_RATIO", config.risk_reward_ratio)
    config.max_open_positions = _read_int(
        "BOT_MAX_OPEN_POSITIONS", config.max_open_positions)
    config.max_position_pct = _read_float(
        "BOT_MAX_POSITION_PCT", config.max_position_pct)
    config.stop_loss_pct = _read_float(
        "BOT_STOP_LOSS_PCT", config.stop_loss_pct)
    config.take_profit_pct = _read_float(
        "BOT_TAKE_PROFIT_PCT", config.take_profit_pct)
    config.trailing_stop_pct = _read_float(
        "BOT_TRAILING_STOP_PCT", config.trailing_stop_pct)
    config.max_daily_loss_pct = _read_float(
        "BOT_MAX_DAILY_LOSS_PCT", config.max_daily_loss_pct)
    config.min_margin_buffer_pct = _read_float(
        "BOT_MIN_MARGIN_BUFFER_PCT", config.min_margin_buffer_pct)
    config.quote_asset = os.getenv("BOT_QUOTE_ASSET", config.quote_asset)
    config.initial_equity = _read_float(
        "BOT_INITIAL_EQUITY", config.initial_equity)
    config.poll_seconds = _read_int("BOT_POLL_SECONDS", config.poll_seconds)
    config.strategy_profile = os.getenv(
        "BOT_STRATEGY_PROFILE", config.strategy_profile)
    config.data_dir = os.getenv("BOT_DATA_DIR", config.data_dir)
    config.db_path = os.getenv("BOT_DB_PATH", config.db_path)
    config.binance_base_url = os.getenv(
        "BINANCE_BASE_URL", config.binance_base_url)
    config.testnet = _read_bool("BINANCE_TESTNET", config.testnet)
    config.live_trading_confirmed = _read_bool(
        "BOT_LIVE_TRADING_CONFIRMED", config.live_trading_confirmed)
    config.api_key = os.getenv("BINANCE_API_KEY", "")
    config.api_secret = os.getenv("BINANCE_API_SECRET", "")
    if config.testnet:
        config.binance_base_url = os.getenv(
            "BINANCE_TESTNET_URL", "https://testnet.binancefuture.com")
    return config


def load_strategy_profile(config: BotConfig, name: str | None = None) -> StrategyProfile:
    profile_name = name or config.strategy_profile
    path = strategy_profile_path(config, profile_name)
    if path.exists():
        return StrategyProfile.from_dict(json.loads(path.read_text()))
    return default_strategy_profile(profile_name)


def save_strategy_profile(config: BotConfig, profile: StrategyProfile) -> Path:
    path = strategy_profile_path(config, profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
    return path


def strategy_profile_path(config: BotConfig, name: str) -> Path:
    return config_dir(config).joinpath("strategies", f"{name}.json")


def default_strategy_profile(name: str = "default") -> StrategyProfile:
    return StrategyProfile(
        name=name,
        threshold=0.35,
        description="Balanced multi-strategy profile using EMA, MACD, RSI, Bollinger Bands, and ADX.",
        rules=[
            StrategyRule(name="ema_cross", weight=1.4, params={
                         "fast_period": 9, "slow_period": 21}),
            StrategyRule(name="macd", weight=1.1, params={
                         "fast_period": 12, "slow_period": 26, "signal_period": 9}),
            StrategyRule(name="rsi", weight=0.8, params={
                         "period": 14, "oversold": 30, "overbought": 70}),
            StrategyRule(name="bollinger", weight=0.8, params={
                         "period": 20, "std_dev": 2.0}),
            StrategyRule(name="adx", weight=0.9, params={
                         "period": 14, "threshold": 22}),
        ],
    )


def _read_bool(env_name: str, default: bool) -> bool:
    value = os.getenv(env_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_int(env_name: str, default: int) -> int:
    value = os.getenv(env_name)
    return default if value is None or not value.strip() else int(value)


def _read_float(env_name: str, default: float) -> float:
    value = os.getenv(env_name)
    return default if value is None or not value.strip() else float(value)
