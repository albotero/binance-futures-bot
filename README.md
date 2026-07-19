# Futures Bot

A Python Binance futures trading bot with:

- Binance API connection via environment variables
- Paper trading by default, live trading when explicitly enabled
- Built-in technical analysis strategies
- Symbol filtering for selected pairs or the full USDT futures universe
- Long-only or long/short operation
- Local web dashboard for monitoring and manual trade control
- Strategy profiles saved to disk for reuse and sharing
- SQLite trade history and snapshot storage

## Important

This project is a trading tool, not financial advice. Futures trading is high risk and can liquidate your account quickly. Start in `paper` mode first and verify every setting before using `live` mode.

The bot defaults to paper trading and will not place real orders unless you set `BOT_MODE=live` and provide valid Binance API credentials.

## Features

- EMA crossing
- MACD histogram and crossover behavior
- RSI mean reversion
- Bollinger band breakout/reversion
- ADX trend filtering
- Weighted multi-strategy profiles
- Stop loss, take profit, and trailing stop support
- Trade history in SQLite
- Manual close for any open trade from the dashboard
- Save and load strategy profiles from disk
- Paper-trading backtest suite and strategy comparison reports

## Installation

1. Create and activate a virtual environment.
2. Install the dependencies.
3. Copy `.env.example` to `.env` and fill in your Binance keys.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Environment

Set these values in `.env`:

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `BOT_MODE` as `paper` or `live`
- `BOT_SYMBOLS` for a comma-separated list of pairs
- `BOT_TRADE_ALL_SYMBOLS=true` to scan all USDT futures pairs
- `BOT_ALLOW_SHORT=true` to enable short positions
- `BOT_LEVERAGE` and `BOT_MAX_LEVERAGE` to set leverage controls

The rest of the settings are optional and can be tuned for risk, leverage, and behavior.

## Run

Start the monitoring dashboard and API:

```bash
futures-bot run-web
```

Or run the bot loop without the web UI:

```bash
futures-bot run-bot
```

Run a backtest from the CLI:

```bash
futures-bot backtest --profile default --symbol BTCUSDT --symbol ETHUSDT
futures-bot backtest --compare ema_cross macd rsi --symbol BTCUSDT
```

The dashboard is available at `http://127.0.0.1:8000`.

The dashboard also includes a Backtest Runner panel where you can:

- run one saved profile on selected symbols
- compare multiple built-in strategies in one run
- view PnL, win rate, and max drawdown summaries
- store report JSON files under `data/reports/`

## Strategy Profiles

Strategy profiles are saved under `data/strategies/` as JSON files. You can create a profile by posting to the dashboard API or by editing the JSON file directly. Profiles can be copied to another computer and loaded there.

The default profile combines EMA, MACD, RSI, Bollinger Bands, and ADX with weighted voting.

## Manual Control

From the dashboard you can:

- start or stop the engine
- run one cycle manually
- pause and resume processing
- close any open trade manually
- inspect signal scores and reasons

## Risk Controls

The bot includes:

- per-trade risk sizing based on configured equity percentage
- leverage-aware position sizing
- stop loss, take profit, and trailing stop logic
- max open positions
- long-only mode if you do not want shorts

For live-trading safety, set and review:

- `BINANCE_TESTNET=true` to use Binance Futures testnet
- `BOT_LIVE_TRADING_CONFIRMED=true` before any real live orders
- `BOT_MAX_DAILY_LOSS_PCT`, `BOT_MAX_POSITION_PCT`, `BOT_MIN_MARGIN_BUFFER_PCT`, and `BOT_MAX_LEVERAGE`

Even with these controls, live futures trading can lose money quickly. Use conservative settings.

## Suggested Next Steps

1. Test in paper mode on a few symbols only.
2. Adjust the strategy profile and risk settings.
3. Create a Binance testnet or small-cap live configuration before scaling up.
