from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .backtest import compare_profiles, run_backtest_suite, save_backtest_report
from .config import default_strategy_profile, load_bot_config, load_strategy_profile
from .engine import TradingEngine
from .models import BotConfig, StrategyProfile


def build_app(engine: TradingEngine | None = None) -> FastAPI:
    config = load_bot_config()
    bot = engine or TradingEngine(config)
    app = FastAPI(title="Futures Bot", version="0.1.0")

    static_dir = Path(__file__).resolve().parent / "dashboard" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (static_dir / "index.html").read_text()

    @app.get("/api/status")
    def status() -> dict:
        return bot.status()

    @app.get("/api/trades")
    def trades(limit: int = 200) -> dict:
        return {"trades": bot.storage.list_trades(limit=limit)}

    @app.get("/api/history")
    def history(limit: int = 2000) -> dict:
        return {"snapshots": bot.storage.list_snapshots(limit=limit)}

    @app.get("/api/exchange")
    def exchange_view() -> dict:
        symbols = bot.list_symbols()[:8]
        prices: dict[str, float | None] = {}
        for symbol in symbols:
            try:
                prices[symbol] = bot.data.latest_price(symbol)
            except Exception:  # noqa: BLE001
                prices[symbol] = None
        return {
            "mode": bot.config.mode,
            "testnet": bot.config.testnet,
            "base_url": bot.config.binance_base_url,
            "quote_asset": bot.config.quote_asset,
            "interval": bot.config.interval,
            "candle_style": bot.config.candle_style,
            "leverage": bot.config.leverage,
            "max_leverage": bot.config.max_leverage,
            "tracked_symbols": symbols,
            "latest_prices": prices,
        }

    @app.post("/api/start")
    def start() -> dict:
        bot.start()
        return {"ok": True}

    @app.post("/api/stop")
    def stop() -> dict:
        bot.stop()
        return {"ok": True}

    @app.post("/api/pause")
    def pause() -> dict:
        bot.pause()
        return {"ok": True}

    @app.post("/api/resume")
    def resume() -> dict:
        bot.resume()
        return {"ok": True}

    @app.post("/api/trades/{symbol}/close")
    def close_trade(symbol: str) -> dict:
        result = bot.manually_close(symbol.upper())
        if not result:
            raise HTTPException(
                status_code=404, detail="No open position for symbol")
        return {"ok": True, "trade": result.to_dict()}

    @app.get("/api/strategies")
    def strategies() -> dict:
        strategy_dir = Path(bot.config.data_dir) / "strategies"
        strategy_dir.mkdir(parents=True, exist_ok=True)
        saved = [path.stem for path in strategy_dir.glob("*.json")]
        return {"active": bot.profile.to_dict(), "saved": saved}

    @app.post("/api/strategies/save")
    def save_strategy(profile: dict) -> dict:
        parsed_profile = StrategyProfile.from_dict(profile)
        path = bot.save_profile(parsed_profile)
        return {"ok": True, "path": str(path)}

    @app.post("/api/strategies/load/{name}")
    def load_strategy(name: str) -> dict:
        if bot.state.running:
            raise HTTPException(
                status_code=409,
                detail="Stop the bot before changing strategy profile",
            )
        profile = bot.reload_profile(name)
        return {"ok": True, "profile": profile.to_dict()}

    @app.post("/api/run-once")
    def run_once() -> dict:
        bot.run_once()
        return {"ok": True}

    @app.get("/api/config")
    def config_view() -> dict:
        return bot.config.to_dict()

    @app.post("/api/seed-default-strategy")
    def seed_default_strategy() -> dict:
        profile = default_strategy_profile()
        path = bot.save_profile(profile)
        return {"ok": True, "path": str(path)}

    @app.post("/api/backtest/run")
    def backtest_run(payload: dict | None = None) -> dict:
        body = payload or {}
        raw_symbols = body.get("symbols", bot.config.symbols)
        if isinstance(raw_symbols, str):
            symbols = [item.strip().upper()
                       for item in raw_symbols.split(",") if item.strip()]
        else:
            symbols = [str(symbol).upper()
                       for symbol in raw_symbols if str(symbol).strip()]
        if not symbols:
            symbols = list(bot.config.symbols)

        profile_name = str(body.get("profile", bot.config.strategy_profile)).strip(
        ) or bot.config.strategy_profile
        raw_compare = body.get("compare", [])
        if isinstance(raw_compare, str):
            compare = [item.strip()
                       for item in raw_compare.split(",") if item.strip()]
        else:
            compare = [str(item).strip()
                       for item in raw_compare if str(item).strip()]

        interval = str(body.get("interval", bot.config.interval)
                       ).strip() or bot.config.interval
        raw_candles_limit = body.get("candles_limit", bot.config.candles_limit)
        raw_leverage = body.get("leverage", bot.config.leverage)
        try:
            candles_limit = int(raw_candles_limit)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="candles_limit must be an integer",
            ) from exc
        try:
            leverage = int(raw_leverage)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="leverage must be an integer",
            ) from exc
        if candles_limit < 30 or candles_limit > 1500:
            raise HTTPException(
                status_code=400,
                detail="candles_limit must be between 30 and 1500",
            )
        if leverage < 1 or leverage > 125:
            raise HTTPException(
                status_code=400,
                detail="leverage must be between 1 and 125",
            )

        run_config = BotConfig.from_dict(
            {
                **bot.config.to_dict(),
                "interval": interval,
                "candles_limit": candles_limit,
                "leverage": leverage,
                "max_leverage": leverage,
            }
        )

        try:
            profile = bot.profile if profile_name == bot.profile.name else load_strategy_profile(
                run_config, profile_name)
            if compare:
                report = compare_profiles(run_config, compare, symbols)
            else:
                report = run_backtest_suite(run_config, [profile], symbols)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"Backtest failed: {exc}",
            ) from exc

        path = save_backtest_report(report, run_config.data_dir)
        return {
            "ok": True,
            "path": str(path),
            "report": report.to_dict(),
            "context": {
                "profile": profile_name,
                "compare": compare,
                "symbols": symbols,
                "interval": interval,
                "candles_limit": candles_limit,
                "leverage": leverage,
            },
        }

    return app
