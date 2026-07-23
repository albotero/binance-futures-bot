from __future__ import annotations

import argparse
import time

import uvicorn

from .backtest import compare_profiles, prefetch_backtest_cache, run_backtest_suite, save_backtest_report
from .api import build_app
from .config import default_strategy_profile, load_bot_config, load_strategy_profile, save_strategy_profile
from .engine import TradingEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance futures trading bot")
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser(
        "run-web", help="Start the monitoring dashboard and API")
    subparsers.add_parser(
        "run-bot", help="Run the trading engine without the dashboard")
    subparsers.add_parser(
        "seed-strategy", help="Write the default strategy profile to disk")
    backtest_parser = subparsers.add_parser(
        "backtest", help="Run a paper backtest and save a report")
    backtest_parser.add_argument(
        "--profile", default="default", help="Saved strategy profile name to backtest")
    backtest_parser.add_argument(
        "--compare", nargs="*", default=[], help="Built-in strategy names to compare")
    backtest_parser.add_argument(
        "--symbol", action="append", dest="symbols", help="Symbol to include in the backtest")
    backtest_parser.add_argument(
        "--all-symbols",
        action="store_true",
        help="Backtest all available symbols for BOT_QUOTE_ASSET",
    )
    backtest_parser.add_argument(
        "--duration",
        default=None,
        help="Backtest duration such as 4w, 6mo, 1y, or 1y6mo",
    )

    warm_parser = subparsers.add_parser(
        "warm-backtest-cache", help="Download and cache backtest candles for faster subsequent runs")
    warm_parser.add_argument(
        "--symbol", action="append", dest="symbols", help="Symbol to include in cache warm")
    warm_parser.add_argument(
        "--all-symbols",
        action="store_true",
        help="Warm cache for all available symbols of BOT_QUOTE_ASSET",
    )
    warm_parser.add_argument(
        "--duration",
        default=None,
        help="Duration such as 4w, 6mo, 1y, or 1y6mo",
    )

    subparsers.add_parser(
        "sync-exchange-history",
        help="Backfill local trade history from Binance user-trade fills",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_bot_config()

    if args.command == "seed-strategy":
        profile = default_strategy_profile(config.strategy_profile)
        path = save_strategy_profile(config, profile)
        print(f"Saved strategy profile to {path}")
        return

    if args.command == "backtest":
        if args.duration:
            config.backtest_duration = args.duration
        selected_symbols = [symbol.upper()
                            for symbol in (args.symbols or config.symbols)]
        if args.compare:
            report = compare_profiles(
                config,
                args.compare,
                None if args.all_symbols else selected_symbols,
                all_symbols=args.all_symbols,
            )
        else:
            profile = load_strategy_profile(config, args.profile)
            report = run_backtest_suite(
                config,
                [profile],
                None if args.all_symbols else selected_symbols,
                all_symbols=args.all_symbols,
            )
        path = save_backtest_report(report, config.data_dir)
        print(f"Saved backtest report to {path}")
        for item in report.reports:
            print(f"{item.profile_name}: pnl={item.net_pnl:.2f} win_rate={item.win_rate:.1f}% drawdown={item.max_drawdown:.1f}%")
        return

    if args.command == "warm-backtest-cache":
        if args.duration:
            config.backtest_duration = args.duration
        selected_symbols = [symbol.upper()
                            for symbol in (args.symbols or config.symbols)]
        result = prefetch_backtest_cache(
            config,
            symbols=None if args.all_symbols else selected_symbols,
            all_symbols=args.all_symbols,
        )
        print(
            f"Backtest cache ready: symbols={result['symbols']} loaded={result['loaded']} fetched={result['fetched']}"
        )
        return

    engine = TradingEngine(config)

    if args.command == "sync-exchange-history":
        result = engine.sync_exchange_history()
        print(
            f"Exchange history sync complete: trades={result['trades']} updated={result['updated']}"
        )
        return

    if args.command == "run-bot":
        engine.start()
        print("Trading engine started. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            engine.stop()
        return

    app = build_app(engine)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
