from __future__ import annotations

import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .backtest import compare_profiles, parse_backtest_duration, run_backtest_suite, save_backtest_report
from .config import default_strategy_profile, load_bot_config, load_strategy_profile
from .engine import TradingEngine
from .models import BotConfig, StrategyProfile


def build_app(engine: TradingEngine | None = None) -> FastAPI:
    config = load_bot_config()
    bot = engine or TradingEngine(config)
    app = FastAPI(title="Futures Bot", version="0.1.0")
    backtest_jobs: dict[str, dict[str, object]] = {}
    backtest_jobs_lock = threading.Lock()

    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def snapshot_job(job_id: str) -> dict[str, object] | None:
        with backtest_jobs_lock:
            job = backtest_jobs.get(job_id)
            return dict(job) if job else None

    def update_job(job_id: str, **updates: object) -> dict[str, object] | None:
        with backtest_jobs_lock:
            job = backtest_jobs.get(job_id)
            if not job:
                return None
            job.update(updates)
            job["updated_at"] = now_iso()
            return dict(job)

    def create_job(payload: dict[str, object]) -> dict[str, object]:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "progress": 0,
            "message": "Queued",
            "cancel_requested": False,
            "context": payload,
            "result": None,
            "error": None,
        }
        with backtest_jobs_lock:
            backtest_jobs[job_id] = job
        return dict(job)

    def backtest_progress(job_id: str, payload: dict[str, object]) -> None:
        job = snapshot_job(job_id)
        if not job or job.get("status") == "failed":
            return

        stage = str(payload.get("stage", "")).strip()
        message = "Running backtest"
        progress = float(job.get("progress") or 0)

        if stage == "profile_start":
            profile = str(payload.get("profile", "default"))
            profile_index = int(payload.get("profile_index", 1) or 1)
            profile_total = int(payload.get("profile_total", 1) or 1)
            symbol_total = int(payload.get("symbol_total", 0) or 0)
            message = f"Running profile {profile} ({profile_index}/{profile_total})"
            progress = ((profile_index - 1) / max(profile_total, 1)) * 100
            update_job(
                job_id,
                status="running",
                started_at=job.get("started_at") or now_iso(),
                progress=round(progress, 1),
                message=message,
                stage=stage,
                profile=profile,
                profile_index=profile_index,
                profile_total=profile_total,
                symbol_total=symbol_total,
            )
            return

        if stage == "symbol":
            profile = str(payload.get("profile", "default"))
            symbol = str(payload.get("symbol", ""))
            completed = int(payload.get("completed", 0) or 0)
            total = int(payload.get("total", 1) or 1)
            profile_index = int(job.get("profile_index") or 1)
            profile_total = int(job.get("profile_total") or 1)
            progress = (((profile_index - 1) / max(profile_total, 1)) +
                        (completed / max(total, 1)) / max(profile_total, 1)) * 100
            message = f"{profile}: {symbol or 'symbol'} ({completed}/{total})"
            update_job(
                job_id,
                status="running",
                started_at=job.get("started_at") or now_iso(),
                progress=round(progress, 1),
                message=message,
                stage=stage,
                profile=profile,
                symbol=symbol,
                completed=completed,
                total=total,
            )
            return

        if stage == "profile_complete":
            profile = str(payload.get("profile", "default"))
            profile_index = int(payload.get(
                "profile_index", job.get("profile_index") or 1) or 1)
            profile_total = int(payload.get(
                "profile_total", job.get("profile_total") or 1) or 1)
            message = f"Completed profile {profile} ({profile_index}/{profile_total})"
            progress = (profile_index / max(profile_total, 1)) * 100
            update_job(
                job_id,
                status="running",
                progress=round(progress, 1),
                message=message,
                stage=stage,
                profile=profile,
                profile_index=profile_index,
                profile_total=profile_total,
            )
            return

        update_job(
            job_id,
            status="running",
            started_at=job.get("started_at") or now_iso(),
            progress=round(progress, 1),
            message=message,
            stage=stage or "running",
        )

    def finalize_job(job_id: str, *, result: dict[str, object] | None = None, error: str | None = None) -> None:
        if error:
            update_job(
                job_id,
                status="failed",
                finished_at=now_iso(),
                progress=0,
                message="Backtest failed",
                error=error,
                result=None,
            )
            return
        update_job(
            job_id,
            status="completed",
            finished_at=now_iso(),
            progress=100,
            message="Backtest complete",
            error=None,
            result=result,
        )

    def request_cancel(job_id: str) -> dict[str, object] | None:
        with backtest_jobs_lock:
            job = backtest_jobs.get(job_id)
            if not job:
                return None
            if job.get("status") in {"completed", "failed", "canceled"}:
                return dict(job)
            job["cancel_requested"] = True
            job["status"] = "cancel_requested"
            job["message"] = "Cancel requested"
            job["updated_at"] = now_iso()
            return dict(job)

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
        all_symbols = _as_bool(body.get("all_symbols", False))
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
        raw_duration = str(
            body.get("duration", bot.config.backtest_duration)).strip()
        raw_leverage = body.get("leverage", bot.config.leverage)
        try:
            leverage = int(raw_leverage)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="leverage must be an integer",
            ) from exc
        try:
            parse_backtest_duration(raw_duration)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if leverage < 1 or leverage > 125:
            raise HTTPException(
                status_code=400,
                detail="leverage must be between 1 and 125",
            )

        run_config = BotConfig.from_dict(
            {
                **bot.config.to_dict(),
                "interval": interval,
                "backtest_duration": raw_duration,
                "leverage": leverage,
                "max_leverage": leverage,
            }
        )

        try:
            profile = bot.profile if profile_name == bot.profile.name else load_strategy_profile(
                run_config, profile_name)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        run_symbols = None if all_symbols else symbols
        job_context = {
            "profile": profile_name,
            "compare": compare,
            "symbols": symbols,
            "all_symbols": all_symbols,
            "quote_asset": run_config.quote_asset,
            "interval": interval,
            "duration": raw_duration,
            "leverage": leverage,
        }
        job = create_job(job_context)
        job_id = str(job["job_id"])

        class BacktestJobCanceled(Exception):
            pass

        def mark_canceled() -> None:
            update_job(
                job_id,
                status="canceled",
                finished_at=now_iso(),
                progress=min(
                    float((snapshot_job(job_id) or {}).get("progress") or 0), 99.0),
                message="Backtest canceled",
                error=None,
                result=None,
            )

        def raise_if_canceled() -> None:
            if snapshot_job(job_id) and snapshot_job(job_id).get("cancel_requested"):
                raise BacktestJobCanceled()

        def worker() -> None:
            update_job(
                job_id,
                status="running",
                started_at=now_iso(),
                message="Backtest started",
            )
            try:
                raise_if_canceled()

                def progress(payload: dict[str, object]) -> None:
                    backtest_progress(job_id, payload)
                    raise_if_canceled()

                if compare:
                    report = compare_profiles(
                        run_config,
                        compare,
                        run_symbols,
                        all_symbols=all_symbols,
                        progress_callback=progress,
                    )
                else:
                    report = run_backtest_suite(
                        run_config,
                        [profile],
                        run_symbols,
                        all_symbols=all_symbols,
                        progress_callback=progress,
                    )

                raise_if_canceled()
                path = save_backtest_report(report, run_config.data_dir)
                resolved_symbols = symbols
                if all_symbols and report.reports:
                    resolved_symbols = [
                        item.symbol for item in report.reports[0].symbol_reports]
                result = {
                    "path": str(path),
                    "report": report.to_dict(),
                    "context": {
                        "profile": profile_name,
                        "compare": compare,
                        "symbols": resolved_symbols,
                        "all_symbols": all_symbols,
                        "quote_asset": run_config.quote_asset,
                        "interval": interval,
                        "duration": raw_duration,
                        "leverage": leverage,
                    },
                }
                raise_if_canceled()
                finalize_job(job_id, result=result)
            except BacktestJobCanceled:
                mark_canceled()
            except Exception as exc:  # noqa: BLE001
                finalize_job(job_id, error=str(exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return {
            "ok": True,
            "job_id": job_id,
            "status_url": f"/api/backtest/jobs/{job_id}",
            "context": job_context,
        }

    @app.get("/api/backtest/jobs/{job_id}")
    def backtest_job(job_id: str) -> dict:
        job = snapshot_job(job_id)
        if not job:
            raise HTTPException(
                status_code=404, detail="Backtest job not found")
        return {"ok": True, "job": job}

    @app.post("/api/backtest/jobs/{job_id}/cancel")
    def cancel_backtest_job(job_id: str) -> dict:
        job = request_cancel(job_id)
        if not job:
            raise HTTPException(
                status_code=404, detail="Backtest job not found")
        if job.get("status") in {"completed", "failed", "canceled"}:
            raise HTTPException(
                status_code=409, detail="Backtest job is already finished")
        return {"ok": True, "job": job}

    return app


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
