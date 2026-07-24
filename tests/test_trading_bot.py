from __future__ import annotations
from futures_bot.storage import SQLiteStore
from futures_bot.api import build_app
from futures_bot.strategies.engine import StrategyEvaluation, evaluate_profile
from futures_bot.strategies.builtins import AdxStrategy, BollingerStrategy, EmaCrossStrategy, MacdStrategy, RsiReversionStrategy, heikin_ashi
from futures_bot.models import BotConfig, Position, Side, StrategyProfile, StrategyRule, TradeStatus
from futures_bot.execution_paper import PaperExecution
from futures_bot.execution_live import BinanceFuturesExecution, _group_exchange_fills, _match_trade_to_fills
from futures_bot.engine import TradingEngine
from futures_bot.config import default_strategy_profile
from futures_bot.backtest import BacktestReport, BacktestSymbolReport, BacktestSuiteResult, _open_backtest_position, compare_profiles, fetch_backtest_candles, parse_backtest_duration, run_backtest_suite
from futures_bot.market_data import BinanceAPIError

import tempfile
import unittest
import time
from pathlib import Path
import sys
from unittest.mock import patch
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def make_candles(closes: list[float]) -> list[dict[str, float]]:
    candles: list[dict[str, float]] = []
    for close in closes:
        candles.append({
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 100.0,
        })
    return candles


class IndicatorTests(unittest.TestCase):
    def test_heikin_ashi_transforms_prices(self) -> None:
        candles = [
            {"open": 10.0, "high": 14.0, "low": 8.0,
                "close": 12.0, "volume": 100.0},
            {"open": 12.0, "high": 16.0, "low": 11.0,
                "close": 15.0, "volume": 100.0},
        ]

        transformed = heikin_ashi(candles)

        self.assertAlmostEqual(transformed[0]["open"], 11.0)
        self.assertAlmostEqual(transformed[0]["close"], 11.0)
        self.assertAlmostEqual(transformed[1]["open"], 11.0)
        self.assertAlmostEqual(transformed[1]["close"], 13.5)

    def test_ema_cross_detects_bullish_trend(self) -> None:
        candles = make_candles([100.0] * 20 + [101.0, 102.0, 104.0, 107.0, 111.0, 116.0, 122.0, 129.0,
                               137.0, 146.0, 156.0, 167.0, 179.0, 192.0, 206.0, 221.0, 237.0, 254.0, 272.0, 291.0])

        signal = EmaCrossStrategy().generate(candles, "BTCUSDT")

        self.assertGreater(signal.score, 0)
        self.assertEqual(signal.side, Side.LONG)

    def test_macd_and_rsi_signals_react_to_trend(self) -> None:
        uptrend = make_candles([float(index) for index in range(1, 60)])
        downtrend = make_candles([float(index) for index in range(60, 1, -1)])

        macd_signal = MacdStrategy().generate(uptrend, "BTCUSDT")
        rsi_signal = RsiReversionStrategy().generate(downtrend, "BTCUSDT")
        bollinger_signal = BollingerStrategy().generate(uptrend, "BTCUSDT")
        adx_signal = AdxStrategy().generate(uptrend, "BTCUSDT")

        self.assertNotEqual(macd_signal.score, 0)
        self.assertGreater(rsi_signal.score, 0)
        self.assertIsNotNone(bollinger_signal.reason)
        self.assertIsNotNone(adx_signal.reason)

    def test_profile_evaluation_builds_directional_exit_plan(self) -> None:
        profile = StrategyProfile(
            name="ha-trend",
            threshold=0.3,
            rules=[
                StrategyRule(name="ema_cross", params={
                             "fast_period": 7, "slow_period": 21, "candle_style": "heikin_ashi"}),
                StrategyRule(name="macd", params={
                             "candle_style": "heikin_ashi"}),
                StrategyRule(name="adx", params={
                             "candle_style": "heikin_ashi"}),
            ],
        )
        candles = make_candles(
            [100.0] * 20 + [101.0, 102.0, 104.0, 106.0, 109.0, 113.0, 118.0, 124.0, 131.0, 139.0])

        evaluation = evaluate_profile(profile, candles, "BTCUSDT")

        self.assertEqual(evaluation.action, "long")
        self.assertIsNotNone(evaluation.exit_plan)
        self.assertLess(evaluation.exit_plan.stop_loss_price,
                        candles[-1]["close"])
        self.assertGreater(
            evaluation.exit_plan.take_profit_price, candles[-1]["close"])


class EngineTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = Path(self.temp_dir.name) / "bot.db"
        self.config = BotConfig(
            mode="paper",
            initial_equity=1000.0,
            db_path=str(db_path),
            data_dir=str(Path(self.temp_dir.name) / "data"),
            max_open_positions=2,
            max_position_pct=50.0,
            leverage=2,
            max_leverage=2,
            allow_short=True,
            risk_reward_ratio=2.2,
        )
        self.engine = TradingEngine(self.config)
        self.engine.execution = PaperExecution(
            initial_equity=self.config.initial_equity,
            trailing_stop_pct=self.config.trailing_stop_pct,
        )
        self.engine.storage = SQLiteStore(db_path)
        self.engine.profile = StrategyProfile(
            name="test-profile",
            threshold=0.3,
            rules=[StrategyRule(name="ema_cross", params={})],
        )

    def test_position_reversal_closes_and_reopens(self) -> None:
        self.engine._open_from_signal("BTCUSDT", 100.0, "long")

        evaluation = StrategyEvaluation(
            score=-1.0, action="short", reasons=["flip"], signals=[])
        self.engine._sync_position("BTCUSDT", 99.6, evaluation)

        position = self.engine.execution.get_position("BTCUSDT")
        self.assertIsNotNone(position)
        self.assertEqual(position.side, Side.SHORT)

        trades = self.engine.storage.list_trades()
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["status"], TradeStatus.OPEN.value)
        self.assertNotEqual(trades[1]["status"], TradeStatus.OPEN.value)

    def test_daily_loss_guard_blocks_new_trades(self) -> None:
        self.engine.execution.realized_pnl = -100.0
        self.engine._open_from_signal("ETHUSDT", 100.0, "long")

        self.assertTrue(self.engine.state.paused)
        self.assertIsNone(self.engine.execution.get_position("ETHUSDT"))

    def test_open_from_signal_uses_strategy_exit_plan(self) -> None:
        evaluation = StrategyEvaluation(
            score=1.0,
            action="long",
            reasons=["trend"],
            signals=[],
            exit_plan=None,
        )
        evaluation.exit_plan = type("Plan", (), {
            "stop_loss_price": 96.0,
            "take_profit_price": 109.0,
            "trailing_stop_price": 97.5,
        })()

        self.engine._open_from_signal("BTCUSDT", 100.0, "long", evaluation)

        position = self.engine.execution.get_position("BTCUSDT")
        self.assertIsNotNone(position)
        self.assertEqual(position.stop_loss_price, 96.0)
        self.assertEqual(position.take_profit_price, 109.0)
        self.assertEqual(position.trailing_stop_price, 97.5)

    def test_leverage_expands_position_size_when_cap_binds(self) -> None:
        evaluation = StrategyEvaluation(
            score=1.0,
            action="long",
            reasons=["trend"],
            signals=[],
            exit_plan=None,
        )
        evaluation.exit_plan = type("Plan", (), {
            "stop_loss_price": 99.5,
            "take_profit_price": 101.5,
            "trailing_stop_price": 99.7,
        })()

        self.engine.config.max_position_pct = 25.0
        self.engine.config.leverage = 2
        self.engine.config.max_leverage = 2
        self.engine._open_from_signal("BTCUSDT", 100.0, "long", evaluation)
        low_leverage_position = self.engine.execution.get_position("BTCUSDT")
        self.assertIsNotNone(low_leverage_position)
        low_leverage_qty = low_leverage_position.quantity

        self.engine.execution = PaperExecution(
            initial_equity=self.config.initial_equity,
            trailing_stop_pct=self.config.trailing_stop_pct,
        )
        self.engine.config.leverage = 10
        self.engine.config.max_leverage = 10
        self.engine._open_from_signal("ETHUSDT", 100.0, "long", evaluation)
        high_leverage_position = self.engine.execution.get_position("ETHUSDT")
        self.assertIsNotNone(high_leverage_position)

        self.assertGreater(high_leverage_position.quantity, low_leverage_qty)
        self.assertAlmostEqual(low_leverage_qty, 5.0, places=5)
        self.assertAlmostEqual(high_leverage_position.quantity, 20.0, places=5)

    def test_fallback_take_profit_uses_risk_reward_ratio(self) -> None:
        self.engine._open_from_signal("BTCUSDT", 100.0, "long", None)

        position = self.engine.execution.get_position("BTCUSDT")
        self.assertIsNotNone(position)
        risk = position.entry_price - position.stop_loss_price
        reward = position.take_profit_price - position.entry_price
        self.assertAlmostEqual(
            reward / risk, self.config.risk_reward_ratio, places=5)

    def test_trailing_stop_moves_with_favorable_price(self) -> None:
        self.engine._open_from_signal("BTCUSDT", 100.0, "long", None)
        position = self.engine.execution.get_position("BTCUSDT")
        self.assertIsNotNone(position)
        original_trailing = position.trailing_stop_price

        evaluation = StrategyEvaluation(
            score=0.0,
            action="hold",
            reasons=["hold"],
            signals=[],
        )
        self.engine._sync_position("BTCUSDT", 102.0, evaluation)

        updated = self.engine.execution.get_position("BTCUSDT")
        self.assertIsNotNone(updated)
        self.assertGreater(updated.trailing_stop_price, original_trailing)

    def test_mark_price_updates_trailing_when_configured(self) -> None:
        execution = PaperExecution(
            initial_equity=1000.0, trailing_stop_pct=1.0)
        execution.open_position(
            Position(
                symbol="BTCUSDT",
                side=Side.LONG,
                quantity=1.0,
                entry_price=100.0,
                current_price=100.0,
                leverage=2,
                strategy="test",
                stop_loss_price=98.0,
                take_profit_price=106.0,
                trailing_stop_price=99.0,
            )
        )

        before = execution.get_position("BTCUSDT")
        self.assertIsNotNone(before)
        before_trailing = before.trailing_stop_price

        execution.mark_price("BTCUSDT", 102.0)

        after = execution.get_position("BTCUSDT")
        self.assertIsNotNone(after)
        self.assertGreater(after.trailing_stop_price, before_trailing)

    def test_staged_trailing_arms_after_activation_and_moves_to_break_even(self) -> None:
        position = Position(
            symbol="BTCUSDT",
            side=Side.LONG,
            quantity=1.0,
            entry_price=100.0,
            current_price=100.0,
            leverage=2,
            strategy="test",
            stop_loss_price=95.0,
            take_profit_price=110.0,
            trailing_stop_price=0.0,
            trailing_stage_enabled=True,
            trailing_break_even_r=0.8,
            trailing_activation_r=1.2,
            trailing_fee_buffer_pct=0.04,
            initial_stop_loss_price=95.0,
        )

        position.mark(101.0)
        position.update_trailing_stop(2.2)
        self.assertFalse(position.trailing_armed)
        self.assertFalse(position.break_even_applied)

        position.mark(104.0)
        position.update_trailing_stop(2.2)
        self.assertFalse(position.trailing_armed)
        self.assertTrue(position.break_even_applied)
        self.assertAlmostEqual(position.stop_loss_price, 100.04, places=6)

        position.mark(106.0)
        position.update_trailing_stop(2.2)
        self.assertTrue(position.trailing_armed)
        self.assertGreater(position.trailing_stop_price, 0.0)

    def test_manual_close_updates_metrics_and_snapshot(self) -> None:
        self.engine._open_from_signal("BTCUSDT", 100.0, "long", None)

        with patch("futures_bot.engine.BinanceMarketData.latest_price", return_value=110.0):
            closed = self.engine.manually_close("BTCUSDT")

        self.assertIsNotNone(closed)
        metrics = self.engine.snapshot()
        self.assertGreater(metrics.realized_pnl, 0.0)
        self.assertGreater(metrics.equity, self.config.initial_equity)

        snapshots = self.engine.storage.list_snapshots(limit=10)
        self.assertTrue(len(snapshots) >= 1)
        latest = snapshots[-1]
        latest_metrics = latest.get("metrics", {})
        self.assertAlmostEqual(
            float(latest_metrics.get("realized_pnl", 0.0)),
            metrics.realized_pnl,
            places=8,
        )

    def test_engine_restores_realized_pnl_from_trade_history(self) -> None:
        self.engine._open_from_signal("ETHUSDT", 100.0, "long", None)
        with patch("futures_bot.engine.BinanceMarketData.latest_price", return_value=110.0):
            self.engine.manually_close("ETHUSDT")

        expected_realized = self.engine.snapshot().realized_pnl
        restored_engine = TradingEngine(self.config)

        self.assertAlmostEqual(
            restored_engine.execution.realized_pnl,
            expected_realized,
            places=8,
        )
        self.assertAlmostEqual(
            restored_engine.execution.balance,
            self.config.initial_equity + expected_realized,
            places=8,
        )

    def test_live_ip_restriction_error_pauses_bot_with_actionable_message(self) -> None:
        live_config = BotConfig(
            mode="live",
            testnet=True,
            initial_equity=1000.0,
            db_path=str(Path(self.temp_dir.name) / "live.db"),
            data_dir=str(Path(self.temp_dir.name) / "live-data"),
            symbols=["BTCUSDT"],
        )
        engine = TradingEngine(live_config)

        def raise_ip_error(symbol: str) -> tuple[float, float]:
            raise BinanceAPIError(
                method="GET",
                path="/fapi/v2/positionRisk",
                status_code=401,
                detail=(
                    "Invalid API-key, IP, or permissions for action, request ip: 205.147.22.18 "
                    "(code -2015). Check API key/secret, Futures trading permission, IP whitelist, "
                    "and testnet/mainnet key alignment."
                ),
                error_code=-2015,
            )

        engine.storage.list_open_trades = lambda: [{
            "id": 1,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quantity": 0.01,
            "entry_price": 100.0,
            "opened_at": "2026-07-24T00:00:00+00:00",
        }]  # type: ignore[method-assign]

        with patch.object(BinanceFuturesExecution, "exchange_position_snapshot", side_effect=raise_ip_error):
            engine.run_once()

        self.assertTrue(engine.state.paused)
        self.assertIn("VPN/public IP likely changed", engine.state.last_error)
        self.assertIn("205.147.22.18", engine.state.last_error)


class BacktestTests(unittest.TestCase):
    def test_parse_backtest_duration_supports_long_ranges(self) -> None:
        duration = parse_backtest_duration("1y6mo2w")
        self.assertEqual(duration.days, 365 + 180 + 14)

    def test_fetch_backtest_candles_respects_max_candles(self) -> None:
        class FakeMarketData:
            def fetch_candles(
                self,
                symbol: str,
                interval: str,
                limit: int = 200,
                start_time: int | None = None,
                end_time: int | None = None,
            ) -> list[dict[str, float]]:
                return [
                    {
                        "open_time": float(index),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0,
                        "volume": 10.0,
                        "close_time": float(index + 1),
                    }
                    for index in range(10)
                ]

        candles = fetch_backtest_candles(
            FakeMarketData(),
            "BTCUSDT",
            "5m",
            "1d",
            max_candles=4,
        )

        self.assertEqual(len(candles), 4)
        self.assertEqual(candles[0]["open_time"], 6.0)

    def test_backtest_uses_bounded_evaluation_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                backtest_duration="4w",
                backtest_eval_window=120,
            )
            profile = default_strategy_profile()
            candles = make_candles(
                [100.0 + index * 0.1 for index in range(450)])
            observed_windows: list[int] = []

            def fake_eval(*args: object, **kwargs: object) -> StrategyEvaluation:
                observed_windows.append(len(args[1]))
                return StrategyEvaluation(
                    score=0.0,
                    action="hold",
                    reasons=[],
                    signals=[],
                    exit_plan=None,
                )

            with patch("futures_bot.backtest.fetch_backtest_candles", return_value=candles):
                with patch("futures_bot.backtest.evaluate_profile", side_effect=fake_eval):
                    run_backtest_suite(config, [profile], ["BTCUSDT"])

            self.assertTrue(observed_windows)
            self.assertLessEqual(max(observed_windows), 120)

    def test_backtest_position_sizing_respects_available_margin_with_leverage(self) -> None:
        config = BotConfig(
            mode="paper",
            initial_equity=1000.0,
            leverage=10,
            max_leverage=10,
            max_position_pct=100.0,
            min_margin_buffer_pct=25.0,
            risk_per_trade_pct=5.0,
            allow_short=True,
        )
        execution = PaperExecution(initial_equity=config.initial_equity)
        profile = StrategyProfile(name="test")
        evaluation = StrategyEvaluation(
            score=1.0,
            action="long",
            reasons=["trend"],
            signals=[],
            exit_plan=type("Plan", (), {
                "stop_loss_price": 99.0,
                "take_profit_price": 103.0,
                "trailing_stop_price": 99.5,
            })(),
        )

        _open_backtest_position(
            config,
            execution,
            profile,
            "BTCUSDT",
            100.0,
            "long",
            evaluation,
        )
        _open_backtest_position(
            config,
            execution,
            profile,
            "ETHUSDT",
            100.0,
            "long",
            evaluation,
        )

        first = execution.get_position("BTCUSDT")
        second = execution.get_position("ETHUSDT")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None

        self.assertAlmostEqual(first.quantity, 50.0, places=6)
        self.assertAlmostEqual(second.quantity, 25.0, places=6)

        snapshot = execution.snapshot()
        self.assertLessEqual(snapshot.margin_in_use, 750.0 + 1e-6)

    def test_backtest_suite_runs_single_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                backtest_duration="4w",
            )
            profile = default_strategy_profile()
            candles = make_candles([float(index) for index in range(1, 90)])
            with patch("futures_bot.backtest.fetch_backtest_candles", return_value=candles):
                report = run_backtest_suite(config, [profile], ["BTCUSDT"])

            self.assertEqual(len(report.reports), 1)
            self.assertEqual(report.reports[0].profile_name, profile.name)

    def test_compare_profiles_builds_single_strategy_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                backtest_duration="4w",
            )
            candles = make_candles([float(index) for index in range(1, 90)])
            with patch("futures_bot.backtest.fetch_backtest_candles", return_value=candles):
                report = compare_profiles(
                    config, ["ema_cross", "macd"], ["BTCUSDT"])

            self.assertEqual([item.profile_name for item in report.reports], [
                             "ema_cross", "macd"])

    def test_backtest_skips_unavailable_symbol_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT", "BADUSDT"],
                backtest_duration="4w",
            )
            profile = default_strategy_profile()
            candles = make_candles([float(index) for index in range(1, 90)])

            def fake_fetch(
                _market_data: object,
                symbol: str,
                interval: str,
                duration: str,
                max_candles: int = 0,
            ) -> list[dict[str, float]]:
                if symbol == "BADUSDT":
                    raise ValueError("symbol not available")
                return candles

            with patch("futures_bot.backtest.fetch_backtest_candles", side_effect=fake_fetch):
                result = run_backtest_suite(
                    config, [profile], ["BTCUSDT", "BADUSDT"])

            report = result.reports[0]
            self.assertEqual(report.requested_symbols, 2)
            self.assertEqual(report.counted_symbols, 1)
            self.assertIn("BADUSDT", report.skipped_symbols)

            bad_symbol_report = next(
                item for item in report.symbol_reports if item.symbol == "BADUSDT")
            self.assertFalse(bad_symbol_report.counted)
            self.assertIn("not available", bad_symbol_report.skip_reason)

    def test_backtest_all_symbols_uses_quote_asset_symbol_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                quote_asset="USDC",
                backtest_duration="4w",
            )
            profile = default_strategy_profile()
            candles = make_candles([float(index) for index in range(1, 90)])

            with patch("futures_bot.backtest.BinanceMarketData.list_symbols", return_value=["BTCUSDC", "ETHUSDC"]):
                with patch("futures_bot.backtest.fetch_backtest_candles", return_value=candles):
                    result = run_backtest_suite(
                        config,
                        [profile],
                        all_symbols=True,
                    )

            report = result.reports[0]
            symbols = [item.symbol for item in report.symbol_reports]
            self.assertEqual(symbols, ["BTCUSDC", "ETHUSDC"])
            self.assertEqual(report.requested_symbols, 2)
            self.assertEqual(report.counted_symbols, 2)

    def test_backtest_run_returns_job_and_polls_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                strategy_profile="default",
            )

            class FakeEngine:
                def __init__(self) -> None:
                    self.config = config
                    self.profile = StrategyProfile(name="default")

            report = BacktestSuiteResult(
                created_at="2026-07-20T00:00:00+00:00",
                reports=[
                    BacktestReport(
                        profile_name="default",
                        created_at="2026-07-20T00:00:00+00:00",
                        start_equity=1000.0,
                        final_equity=1010.0,
                        net_pnl=10.0,
                        win_rate=100.0,
                        max_drawdown=0.0,
                        requested_symbols=1,
                        counted_symbols=1,
                        symbol_reports=[
                            BacktestSymbolReport(
                                symbol="BTCUSDT",
                                final_equity=1010.0,
                                net_pnl=10.0,
                                win_rate=100.0,
                                max_drawdown=0.0,
                            )
                        ],
                    )
                ],
            )

            def fake_run(*args: object, progress_callback=None, **kwargs: object) -> BacktestSuiteResult:
                if progress_callback:
                    progress_callback(
                        {
                            "stage": "profile_start",
                            "profile": "default",
                            "profile_index": 1,
                            "profile_total": 1,
                            "symbol_total": 1,
                        }
                    )
                    progress_callback(
                        {
                            "stage": "symbol",
                            "profile": "default",
                            "symbol": "BTCUSDT",
                            "completed": 1,
                            "total": 1,
                        }
                    )
                    progress_callback(
                        {
                            "stage": "profile_complete",
                            "profile": "default",
                            "profile_index": 1,
                            "profile_total": 1,
                        }
                    )
                return report

            app = build_app(FakeEngine())
            client = TestClient(app)
            report_path = Path(temp_dir) / "report.json"

            with patch("futures_bot.api.run_backtest_suite", side_effect=fake_run):
                with patch("futures_bot.api.save_backtest_report", return_value=report_path):
                    response = client.post(
                        "/api/backtest/run",
                        json={
                            "profile": "default",
                            "symbols": ["BTCUSDT"],
                            "duration": "15d",
                            "interval": "5m",
                            "leverage": 3,
                        },
                    )

                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertTrue(payload["ok"])
                    job_id = payload["job_id"]

                    job = None
                    for _ in range(30):
                        job_response = client.get(
                            f"/api/backtest/jobs/{job_id}")
                        self.assertEqual(job_response.status_code, 200)
                        job = job_response.json()["job"]
                        if job["status"] in {"completed", "failed"}:
                            break
                        time.sleep(0.05)

                    self.assertIsNotNone(job)
                    self.assertEqual(job["status"], "completed")
                    self.assertEqual(job["result"]["path"], str(report_path))
                    self.assertEqual(
                        job["result"]["report"]["reports"][0]["profile_name"], "default")

    def test_backtest_job_can_be_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                strategy_profile="default",
            )

            class FakeEngine:
                def __init__(self) -> None:
                    self.config = config
                    self.profile = StrategyProfile(name="default")

            report = BacktestSuiteResult(
                created_at="2026-07-20T00:00:00+00:00",
                reports=[
                    BacktestReport(
                        profile_name="default",
                        created_at="2026-07-20T00:00:00+00:00",
                        start_equity=1000.0,
                        final_equity=1010.0,
                        net_pnl=10.0,
                        win_rate=100.0,
                        max_drawdown=0.0,
                        requested_symbols=1,
                        counted_symbols=1,
                        symbol_reports=[
                            BacktestSymbolReport(
                                symbol="BTCUSDT",
                                final_equity=1010.0,
                                net_pnl=10.0,
                                win_rate=100.0,
                                max_drawdown=0.0,
                            )
                        ],
                    )
                ],
            )

            def fake_run(*args: object, **kwargs: object) -> BacktestSuiteResult:
                time.sleep(0.2)
                return report

            app = build_app(FakeEngine())
            client = TestClient(app)

            with patch("futures_bot.api.run_backtest_suite", side_effect=fake_run):
                with patch("futures_bot.api.save_backtest_report") as save_mock:
                    response = client.post(
                        "/api/backtest/run",
                        json={
                            "profile": "default",
                            "symbols": ["BTCUSDT"],
                            "duration": "15d",
                            "interval": "5m",
                            "leverage": 3,
                        },
                    )

                    self.assertEqual(response.status_code, 200)
                    job_id = response.json()["job_id"]

                    cancel_response = client.post(
                        f"/api/backtest/jobs/{job_id}/cancel")
                    self.assertEqual(cancel_response.status_code, 200)

                    job = None
                    for _ in range(40):
                        job_response = client.get(
                            f"/api/backtest/jobs/{job_id}")
                        self.assertEqual(job_response.status_code, 200)
                        job = job_response.json()["job"]
                        if job["status"] == "canceled":
                            break
                        time.sleep(0.05)

                    self.assertIsNotNone(job)
                    self.assertEqual(job["status"], "canceled")
                    self.assertEqual(job["message"], "Backtest canceled")
                    save_mock.assert_not_called()


class LiveExecutionTests(unittest.TestCase):
    def test_group_exchange_fills_builds_weighted_average_by_order(self) -> None:
        fills = _group_exchange_fills([
            {"symbol": "NEOUSDC", "orderId": 10, "buyer": True, "qty": "2",
                "quoteQty": "4", "realizedPnl": "0", "time": 1000},
            {"symbol": "NEOUSDC", "orderId": 10, "buyer": True, "qty": "1",
                "quoteQty": "2.1", "realizedPnl": "0", "time": 1000},
        ])

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].order_id, 10)
        self.assertAlmostEqual(fills[0].quantity, 3.0, places=8)
        self.assertAlmostEqual(fills[0].average_price, 2.0333333333, places=8)

    def test_match_trade_to_fills_uses_entry_then_first_opposite_exit(self) -> None:
        fills = _group_exchange_fills([
            {"symbol": "NEOUSDC", "orderId": 10, "buyer": True, "qty": "3",
                "quoteQty": "6", "realizedPnl": "0", "time": 2_000},
            {"symbol": "NEOUSDC", "orderId": 11, "buyer": False, "qty": "3",
                "quoteQty": "6.3", "realizedPnl": "0.3", "time": 3_000},
            {"symbol": "NEOUSDC", "orderId": 12, "buyer": False, "qty": "3",
                "quoteQty": "6.6", "realizedPnl": "0.6", "time": 4_000},
        ])

        entry_fill, exit_fill = _match_trade_to_fills(
            fills,
            trade_side=Side.LONG.value,
            quantity=3.0,
            opened_at_ms=1_500,
        )

        self.assertIsNotNone(entry_fill)
        self.assertIsNotNone(exit_fill)
        self.assertEqual(entry_fill.order_id, 10)
        self.assertEqual(exit_fill.order_id, 11)

    def test_local_only_mode_skips_exchange_protective_orders(self) -> None:
        execution = BinanceFuturesExecution(
            initial_equity=1000.0,
            live_protection_mode="local_only",
        )

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                side = params.get("side")
                if params.get("type") == "MARKET" and side == "BUY":
                    return {"orderId": 100, "avgPrice": "100.0"}
                if params.get("type") == "MARKET" and side == "SELL":
                    return {"orderId": 101, "avgPrice": "101.0"}
                return {"orderId": 199}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                return {"algoId": 999}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        execution.open_position(
            Position(
                symbol="BTCUSDT",
                side=Side.LONG,
                quantity=0.01,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.0,
                take_profit_price=110.0,
                trailing_stop_price=96.0,
            )
        )

        self.assertEqual(fake_client.algo_orders, [])
        self.assertEqual(execution.protective_orders["BTCUSDT"], [])

    def test_open_and_close_places_and_cancels_protective_orders(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []
                self.canceled: list[tuple[str, int]] = []
                self.closed_order_price = 102.5

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 100, "avgPrice": "100.5"}
                if order_type == "MARKET" and side == "SELL":
                    return {"orderId": 104, "avgPrice": str(self.closed_order_price)}
                return {"orderId": 999}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                order_type = params.get("type")
                if order_type == "STOP_MARKET":
                    return {"algoId": 101}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"algoId": 102}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"algoId": 103}
                return {"algoId": 199}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                self.canceled.append((symbol, algo_id))
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        position = execution.open_position(
            Position(
                symbol="BTCUSDT",
                side=Side.LONG,
                quantity=0.01,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.0,
                take_profit_price=110.0,
                trailing_stop_price=96.0,
            )
        )
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.entry_price, 100.5, places=8)
        self.assertAlmostEqual(position.stop_loss_price, 95.5, places=8)
        self.assertAlmostEqual(position.take_profit_price, 110.5, places=8)
        self.assertAlmostEqual(position.trailing_stop_price, 96.5, places=8)
        trailing_order = next(
            item for item in fake_client.algo_orders if item.get("type") == "TRAILING_STOP_MARKET"
        )
        self.assertEqual(trailing_order.get("callbackRate"), 3.98)

        order_types = [item.get("type") for item in fake_client.orders]
        algo_types = [item.get("type") for item in fake_client.algo_orders]
        self.assertIn("MARKET", order_types)
        self.assertIn("STOP_MARKET", algo_types)
        self.assertIn("TAKE_PROFIT_MARKET", algo_types)
        self.assertIn("TRAILING_STOP_MARKET", algo_types)
        self.assertTrue(all(item.get("algoType") ==
                        "CONDITIONAL" for item in fake_client.algo_orders))

        closed = execution.close_position(
            "BTCUSDT", 102.0, TradeStatus.TAKE_PROFIT.value)
        self.assertIsNotNone(closed)
        self.assertAlmostEqual(closed.current_price, 102.5, places=8)
        self.assertAlmostEqual(closed.realized_pnl,
                               (102.5 - 100.5) * 0.01, places=8)
        self.assertEqual(fake_client.canceled, [
                         ("BTCUSDT", 101), ("BTCUSDT", 102), ("BTCUSDT", 103)])

    def test_close_order_failure_keeps_position_and_protection(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []
                self.canceled: list[tuple[str, int]] = []

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 200, "avgPrice": "100.0"}
                if order_type == "MARKET" and side == "SELL":
                    raise RuntimeError("close request failed")
                return {"orderId": 299}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                order_type = params.get("type")
                if order_type == "STOP_MARKET":
                    return {"algoId": 201}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"algoId": 202}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"algoId": 203}
                return {"algoId": 299}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                self.canceled.append((symbol, algo_id))
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        opened = execution.open_position(
            Position(
                symbol="BTCUSDT",
                side=Side.LONG,
                quantity=0.01,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.0,
                take_profit_price=110.0,
                trailing_stop_price=96.0,
            )
        )
        self.assertIsNotNone(opened)
        self.assertIn("BTCUSDT", execution.protective_orders)

        with self.assertRaises(RuntimeError):
            execution.close_position(
                "BTCUSDT", 102.0, TradeStatus.MANUAL_CLOSE.value)

        self.assertIsNotNone(execution.get_position("BTCUSDT"))
        self.assertIn("BTCUSDT", execution.protective_orders)
        self.assertEqual(fake_client.canceled, [])

    def test_trailing_callback_rate_clamped_to_exchange_max(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                return {"orderId": 300, "avgPrice": "100.0"}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                order_type = params.get("type")
                if order_type == "STOP_MARKET":
                    return {"algoId": 301}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"algoId": 302}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"algoId": 303}
                return {"algoId": 300}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        execution.open_position(
            Position(
                symbol="ETHUSDT",
                side=Side.LONG,
                quantity=0.01,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=90.0,
                take_profit_price=120.0,
                trailing_stop_price=80.0,
            )
        )

        trailing_order = next(
            item for item in fake_client.algo_orders if item.get("type") == "TRAILING_STOP_MARKET"
        )
        self.assertEqual(trailing_order.get("callbackRate"), 5.0)

    def test_staged_trailing_skips_exchange_trailing_order(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                return {"orderId": 320, "avgPrice": "100.0"}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                if params.get("type") == "STOP_MARKET":
                    return {"algoId": 321}
                if params.get("type") == "TAKE_PROFIT_MARKET":
                    return {"algoId": 322}
                if params.get("type") == "TRAILING_STOP_MARKET":
                    return {"algoId": 323}
                return {"algoId": 399}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        execution.open_position(
            Position(
                symbol="ETHUSDT",
                side=Side.LONG,
                quantity=0.01,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.0,
                take_profit_price=110.0,
                trailing_stop_price=96.0,
                trailing_stage_enabled=True,
                initial_stop_loss_price=95.0,
            )
        )

        algo_types = [item.get("type") for item in fake_client.algo_orders]
        self.assertIn("STOP_MARKET", algo_types)
        self.assertIn("TAKE_PROFIT_MARKET", algo_types)
        self.assertNotIn("TRAILING_STOP_MARKET", algo_types)

    def test_live_orders_use_symbol_tick_and_step_filters(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []

            def futures_exchange_info(self) -> dict:
                return {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                {"filterType": "MARKET_LOT_SIZE",
                                    "stepSize": "0.001"},
                            ],
                        }
                    ]
                }

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 400, "avgPrice": "100.0"}
                return {"orderId": 499}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                order_type = params.get("type")
                if order_type == "STOP_MARKET":
                    return {"algoId": 401}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"algoId": 402}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"algoId": 403}
                return {"algoId": 499}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        execution.open_position(
            Position(
                symbol="BTCUSDT",
                side=Side.LONG,
                quantity=0.01234,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.57,
                take_profit_price=110.53,
                trailing_stop_price=96.0,
            )
        )

        entry_order = next(
            item for item in fake_client.orders if item.get("type") == "MARKET" and item.get("side") == "BUY"
        )
        stop_order = next(
            item for item in fake_client.algo_orders if item.get("type") == "STOP_MARKET"
        )
        take_profit_order = next(
            item for item in fake_client.algo_orders if item.get("type") == "TAKE_PROFIT_MARKET"
        )

        self.assertEqual(entry_order.get("quantity"), 0.012)
        self.assertEqual(stop_order.get("triggerPrice"), 95.5)
        self.assertEqual(take_profit_order.get("triggerPrice"), 110.6)

    def test_quantity_fallback_rounds_down_when_filters_unavailable(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []

            def futures_exchange_info(self) -> dict:
                raise RuntimeError("exchange info unavailable")

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 500, "avgPrice": "100.0"}
                return {"orderId": 599}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                order_type = params.get("type")
                if order_type == "STOP_MARKET":
                    return {"algoId": 501}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"algoId": 502}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"algoId": 503}
                return {"algoId": 599}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return []

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        execution.open_position(
            Position(
                symbol="SOLUSDT",
                side=Side.LONG,
                quantity=0.012349,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.0,
                take_profit_price=110.0,
                trailing_stop_price=96.0,
            )
        )

        entry_order = next(
            item for item in fake_client.orders if item.get("type") == "MARKET" and item.get("side") == "BUY"
        )
        self.assertEqual(entry_order.get("quantity"), 0.01234)

    def test_external_close_cancels_all_remaining_algo_orders(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.algo_orders: list[dict] = []
                self.canceled: list[int] = []

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                return {"orderId": 800, "avgPrice": "100.0"}

            def futures_place_algo_order(self, **params: dict) -> dict:
                self.algo_orders.append(params)
                if params.get("type") == "STOP_MARKET":
                    return {"algoId": 801}
                if params.get("type") == "TAKE_PROFIT_MARKET":
                    return {"algoId": 802}
                if params.get("type") == "TRAILING_STOP_MARKET":
                    return {"algoId": 803}
                return {"algoId": 899}

            def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
                self.canceled.append(algo_id)
                return {"symbol": symbol, "algoId": algo_id}

            def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                return [{"algoId": 901}, {"algoId": 902}]

            def futures_get_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

        fake_client = FakeClient()
        execution.client = fake_client  # type: ignore[assignment]

        execution.open_position(
            Position(
                symbol="BTCUSDT",
                side=Side.LONG,
                quantity=0.01,
                entry_price=100.0,
                current_price=100.0,
                leverage=3,
                strategy="test",
                stop_loss_price=95.0,
                take_profit_price=110.0,
                trailing_stop_price=96.0,
            )
        )
        closed = execution.close_position_from_exchange(
            "BTCUSDT", 101.0, TradeStatus.EXTERNAL_CLOSE.value)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.status, TradeStatus.EXTERNAL_CLOSE)
        self.assertEqual(sorted(fake_client.canceled),
                         [801, 802, 803, 901, 902])


class LiveEngineReconciliationTests(unittest.TestCase):
    def test_reconcile_marks_stale_open_trade_as_external_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            config = BotConfig(
                mode="live",
                initial_equity=1000.0,
                db_path=str(db_path),
                data_dir=str(Path(temp_dir) / "data"),
                live_trading_confirmed=True,
                symbols=["BTCUSDT"],
            )
            engine = TradingEngine(config)

            class FakeClient:
                def futures_position_risk(self, symbol: str | None = None) -> list[dict]:
                    return [{"symbol": symbol or "BTCUSDT", "positionAmt": "0", "markPrice": "101.0"}]

                def futures_open_algo_orders(self, symbol: str) -> list[dict]:
                    return []

            engine.execution.client = FakeClient()  # type: ignore[assignment]

            engine.storage.record_open(
                Position(
                    symbol="BTCUSDT",
                    side=Side.LONG,
                    quantity=1.0,
                    entry_price=100.0,
                    current_price=100.0,
                    leverage=2,
                    strategy="test",
                    stop_loss_price=98.0,
                    take_profit_price=104.0,
                    trailing_stop_price=99.0,
                )
            )

            engine._reconcile_exchange_positions(["BTCUSDT"])

            open_trades = engine.storage.list_open_trades()
            self.assertEqual(open_trades, [])
            latest = engine.storage.list_trades(limit=1)[0]
            self.assertEqual(latest["status"],
                             TradeStatus.EXTERNAL_CLOSE.value)
            self.assertAlmostEqual(
                float(latest["realized_pnl"]), 1.0, places=8)


if __name__ == "__main__":
    unittest.main()
