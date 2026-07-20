from __future__ import annotations
from futures_bot.storage import SQLiteStore
from futures_bot.strategies.engine import StrategyEvaluation, evaluate_profile
from futures_bot.strategies.builtins import AdxStrategy, BollingerStrategy, EmaCrossStrategy, MacdStrategy, RsiReversionStrategy, heikin_ashi
from futures_bot.models import BotConfig, Side, StrategyProfile, StrategyRule, TradeStatus
from futures_bot.execution_paper import PaperExecution
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
            initial_equity=self.config.initial_equity)
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
            initial_equity=self.config.initial_equity)
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


if __name__ == "__main__":
    unittest.main()
