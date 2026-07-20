from __future__ import annotations
from futures_bot.storage import SQLiteStore
from futures_bot.strategies.engine import StrategyEvaluation, evaluate_profile
from futures_bot.strategies.builtins import AdxStrategy, BollingerStrategy, EmaCrossStrategy, MacdStrategy, RsiReversionStrategy, heikin_ashi
from futures_bot.models import BotConfig, Position, Side, StrategyProfile, StrategyRule, TradeStatus
from futures_bot.execution_paper import PaperExecution
from futures_bot.execution_live import BinanceFuturesExecution
from futures_bot.engine import TradingEngine
from futures_bot.config import default_strategy_profile
from futures_bot.backtest import compare_profiles, run_backtest_suite

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch


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


class BacktestTests(unittest.TestCase):
    def test_backtest_suite_runs_single_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BotConfig(
                mode="paper",
                initial_equity=1000.0,
                data_dir=str(Path(temp_dir) / "data"),
                db_path=str(Path(temp_dir) / "bot.db"),
                symbols=["BTCUSDT"],
                candles_limit=60,
            )
            profile = default_strategy_profile()
            candles = make_candles([float(index) for index in range(1, 90)])
            with patch("futures_bot.backtest.BinanceMarketData.fetch_candles", return_value=candles):
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
                candles_limit=60,
            )
            candles = make_candles([float(index) for index in range(1, 90)])
            with patch("futures_bot.backtest.BinanceMarketData.fetch_candles", return_value=candles):
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
                candles_limit=60,
            )
            profile = default_strategy_profile()
            candles = make_candles([float(index) for index in range(1, 90)])

            def fake_fetch(symbol: str, interval: str, limit: int) -> list[dict[str, float]]:
                if symbol == "BADUSDT":
                    raise ValueError("symbol not available")
                return candles

            with patch("futures_bot.backtest.BinanceMarketData.fetch_candles", side_effect=fake_fetch):
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
                candles_limit=60,
            )
            profile = default_strategy_profile()
            candles = make_candles([float(index) for index in range(1, 90)])

            with patch("futures_bot.backtest.BinanceMarketData.list_symbols", return_value=["BTCUSDC", "ETHUSDC"]):
                with patch("futures_bot.backtest.BinanceMarketData.fetch_candles", return_value=candles):
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


class LiveExecutionTests(unittest.TestCase):
    def test_open_and_close_places_and_cancels_protective_orders(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []
                self.canceled: list[tuple[str, int]] = []
                self.closed_order_price = 102.5

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "STOP_MARKET":
                    return {"orderId": 101}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"orderId": 102}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"orderId": 103}
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 100, "avgPrice": "100.5"}
                if order_type == "MARKET" and side == "SELL":
                    return {"orderId": 104, "avgPrice": str(self.closed_order_price)}
                return {"orderId": 999}

            def futures_cancel_order(self, symbol: str, order_id: int) -> dict:
                self.canceled.append((symbol, order_id))
                return {"symbol": symbol, "orderId": order_id}

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
            item for item in fake_client.orders if item.get("type") == "TRAILING_STOP_MARKET"
        )
        self.assertEqual(trailing_order.get("callbackRate"), 3.98)

        order_types = [item.get("type") for item in fake_client.orders]
        self.assertIn("MARKET", order_types)
        self.assertIn("STOP_MARKET", order_types)
        self.assertIn("TAKE_PROFIT_MARKET", order_types)
        self.assertIn("TRAILING_STOP_MARKET", order_types)

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
                self.canceled: list[tuple[str, int]] = []

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "STOP_MARKET":
                    return {"orderId": 201}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"orderId": 202}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"orderId": 203}
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 200, "avgPrice": "100.0"}
                if order_type == "MARKET" and side == "SELL":
                    raise RuntimeError("close request failed")
                return {"orderId": 299}

            def futures_cancel_order(self, symbol: str, order_id: int) -> dict:
                self.canceled.append((symbol, order_id))
                return {"symbol": symbol, "orderId": order_id}

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

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                if order_type == "STOP_MARKET":
                    return {"orderId": 301}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"orderId": 302}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"orderId": 303}
                return {"orderId": 300, "avgPrice": "100.0"}

            def futures_cancel_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id}

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
            item for item in fake_client.orders if item.get("type") == "TRAILING_STOP_MARKET"
        )
        self.assertEqual(trailing_order.get("callbackRate"), 5.0)

    def test_live_orders_use_symbol_tick_and_step_filters(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []

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
                if order_type == "STOP_MARKET":
                    return {"orderId": 401}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"orderId": 402}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"orderId": 403}
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 400, "avgPrice": "100.0"}
                return {"orderId": 499}

            def futures_cancel_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id}

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
            item for item in fake_client.orders if item.get("type") == "STOP_MARKET"
        )
        take_profit_order = next(
            item for item in fake_client.orders if item.get("type") == "TAKE_PROFIT_MARKET"
        )

        self.assertEqual(entry_order.get("quantity"), 0.012)
        self.assertEqual(stop_order.get("stopPrice"), 95.5)
        self.assertEqual(take_profit_order.get("stopPrice"), 110.6)

    def test_quantity_fallback_rounds_down_when_filters_unavailable(self) -> None:
        execution = BinanceFuturesExecution(initial_equity=1000.0)

        class FakeClient:
            def __init__(self) -> None:
                self.orders: list[dict] = []

            def futures_exchange_info(self) -> dict:
                raise RuntimeError("exchange info unavailable")

            def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
                return {"symbol": symbol, "leverage": leverage}

            def futures_create_order(self, **params: dict) -> dict:
                self.orders.append(params)
                order_type = params.get("type")
                side = params.get("side")
                if order_type == "STOP_MARKET":
                    return {"orderId": 501}
                if order_type == "TAKE_PROFIT_MARKET":
                    return {"orderId": 502}
                if order_type == "TRAILING_STOP_MARKET":
                    return {"orderId": 503}
                if order_type == "MARKET" and side == "BUY":
                    return {"orderId": 500, "avgPrice": "100.0"}
                return {"orderId": 599}

            def futures_cancel_order(self, symbol: str, order_id: int) -> dict:
                return {"symbol": symbol, "orderId": order_id}

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


if __name__ == "__main__":
    unittest.main()
