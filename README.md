# Futures Bot

Binance futures trading bot with:

- Paper mode and live mode
- Dashboard + API controls
- Weighted multi-strategy profiles
- Backtesting and strategy comparison
- SQLite storage for trades and equity snapshots

This project is for education and automation tooling. It is not financial advice.

## Safety First

Futures are high risk. A bad configuration can lose capital quickly.

Before live trading:

1. Run in paper mode first.
2. Test your profile with backtests.
3. Prefer Binance testnet before mainnet.
4. Use conservative leverage and risk-per-trade.

Live protection is enforced in code. Real orders require:

- BOT_MODE=live
- valid BINANCE_API_KEY and BINANCE_API_SECRET
- BOT_LIVE_TRADING_CONFIRMED=true, or BINANCE_TESTNET=true

Protective exits in live mode are placed through Binance algo-order endpoints, not the regular futures order endpoint.

## Features

- Strategy engine with weighted scoring and thresholds
- Built-in indicators:
  - EMA cross
  - MACD (cross + momentum tendency)
  - RSI
  - Bollinger Bands
  - ADX
- Candle styles:
  - raw
  - heikin_ashi
- Dynamic exit planning (support/resistance + volatility)
- Risk-reward target via BOT_RISK_REWARD_RATIO
- Leverage-aware position sizing caps
- Manual close, pause/resume, run-once controls
- Backtest reports saved to data/reports

## Requirements

- Linux/macOS/WSL
- Python 3.10+
- Binance Futures account for live/testnet usage

## Installation

### 1) Clone and enter the project

```bash
git clone <your-repo-url>
cd binance-futures-bot
```

### 2) Create and activate virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

Use either method:

```bash
pip install -r requirements.txt
```

or

```bash
pip install -e .
```

### 4) Configure environment

```bash
cp .env.example .env
```

Edit .env with your preferred settings.

## Quick Start

### Start dashboard and API

```bash
futures-bot run-web
```

Open:

- http://127.0.0.1:8000

### Run engine only (no dashboard)

```bash
futures-bot run-bot
```

### One-time strategy seed (default profile)

```bash
futures-bot seed-strategy
```

## Commands

### Run web dashboard

```bash
futures-bot run-web
```

### Run bot loop in terminal

```bash
futures-bot run-bot
```

### Run backtest for a saved profile

```bash
futures-bot backtest --profile trend_balanced_multi --symbol BTCUSDT --symbol ETHUSDT
```

### Compare built-in strategies in one run

```bash
futures-bot backtest --compare ema_cross macd rsi adx --symbol BTCUSDT
```

Backtest output is saved under data/reports as JSON.

## Environment Configuration

Core variables:

- BINANCE_API_KEY
- BINANCE_API_SECRET
- BOT_MODE (paper or live)
- BOT_SYMBOLS (comma-separated)
- BOT_ALLOW_SHORT
- BOT_TRADE_ALL_SYMBOLS
- BOT_CANDLE_STYLE
- BOT_STRATEGY_PROFILE
- BOT_INTERVAL (example: 1m, 5m, 15m, 1h)
- BOT_CANDLES_LIMIT
- BOT_INITIAL_EQUITY
- BOT_QUOTE_ASSET
- BOT_POLL_SECONDS

Risk and sizing:

- BOT_RISK_PER_TRADE_PCT
- BOT_RISK_REWARD_RATIO
- BOT_LEVERAGE
- BOT_MAX_LEVERAGE
- BOT_MAX_OPEN_POSITIONS
- BOT_MAX_POSITION_PCT
- BOT_STOP_LOSS_PCT
- BOT_TAKE_PROFIT_PCT
- BOT_MAX_DAILY_LOSS_PCT
- BOT_MIN_MARGIN_BUFFER_PCT
- BOT_TRAILING_STOP_PCT

Live safety:

- BOT_LIVE_TRADING_CONFIRMED
- BINANCE_TESTNET
- BINANCE_BASE_URL (optional override)
- BINANCE_TESTNET_URL (optional override)

Storage:

- BOT_DATA_DIR (default: data)
- BOT_DB_PATH (default: data/bot.db)

### Suggested safe baseline

For first real tests:

- BOT_MODE=paper
- BOT_LEVERAGE=2 to 5
- BOT_RISK_PER_TRADE_PCT=0.5 to 1.0
- BOT_MAX_OPEN_POSITIONS=1 to 3
- BOT_MAX_POSITION_PCT=10 to 20

## Dashboard Guide

From the web UI you can:

- Start / stop engine
- Pause / resume loop
- Run one cycle on demand
- Save and load strategy profiles
- Run backtests with interval, candle limit, leverage controls
- See active positions, scores, reasons, and account metrics
- View persisted history chart from DB snapshots

## API Reference

Main endpoints:

- GET /api/status
- GET /api/trades?limit=200
- GET /api/history?limit=2000
- GET /api/exchange
- POST /api/start
- POST /api/stop
- POST /api/pause
- POST /api/resume
- POST /api/run-once
- POST /api/trades/{symbol}/close
- GET /api/strategies
- POST /api/strategies/save
- POST /api/strategies/load/{name}
- POST /api/backtest/run

Example backtest payload:

```json
{
  "profile": "trend_balanced_multi",
  "symbols": ["BTCUSDT", "ETHUSDT"],
  "interval": "5m",
  "candles_limit": 1000,
  "leverage": 5,
  "compare": []
}
```

## Strategy Profiles

Profiles are JSON files in data/strategies.

Each profile has:

- name
- threshold
- description
- rules[]

Each rule has:

- name: ema_cross | macd | rsi | bollinger | adx
- enabled: true/false
- weight: numeric influence
- params: indicator-specific parameters

### Scoring model

Each enabled rule emits a score in [-1, 1].

Final score:

score = sum(rule_score \* weight) / sum(abs(weight))

Action:

- score >= threshold => long
- score <= -threshold => short
- otherwise => hold

Higher threshold means fewer, stricter entries.

### Example custom profile

```json
{
  "name": "my_profile",
  "threshold": 0.72,
  "description": "Balanced trend profile",
  "rules": [
    {
      "name": "ema_cross",
      "enabled": true,
      "weight": 1.2,
      "params": {
        "fast_period": 7,
        "slow_period": 21,
        "candle_style": "heikin_ashi"
      }
    },
    {
      "name": "macd",
      "enabled": true,
      "weight": 1.0,
      "params": {
        "fast_period": 12,
        "slow_period": 26,
        "signal_period": 9,
        "candle_style": "heikin_ashi"
      }
    },
    {
      "name": "adx",
      "enabled": true,
      "weight": 0.75,
      "params": {
        "period": 14,
        "threshold": 20,
        "candle_style": "heikin_ashi"
      }
    }
  ]
}
```

## Included Strategy Profiles

Current profiles in data/strategies include:

- ema7_50_trend_strict
- ema7_50_adx_balanced
- trend_conservative_multi
- trend_balanced_multi
- trend_aggressive_breakout
- mean_reversion_range
- adaptive_trend_guarded

Use one by setting BOT_STRATEGY_PROFILE to the profile name.

## Backtesting Notes

Backtests run in paper execution mode with historical candles and use the same strategy scoring and position logic as runtime.

Best practices:

1. Compare multiple intervals (5m, 15m, 1h).
2. Test several symbol groups.
3. Evaluate both net PnL and drawdown.
4. Re-test after any profile or risk change.

## Data and Files

- data/bot.db
  - trades table
  - snapshots table
- data/strategies/\*.json
- data/reports/\*.json

## Troubleshooting

### ModuleNotFoundError: dotenv

Activate your virtual environment and reinstall dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Profile does not switch from dashboard

The bot must be stopped before loading a different profile.

### No trades are opening

Check:

1. Threshold may be too high.
2. Risk/margin limits may be blocking entries.
3. Strategy may be returning hold for current market.
4. Symbol list and interval may be too restrictive.

## Development

Run tests:

```bash
python -m unittest tests.test_trading_bot
```

Compile check:

```bash
python -m compileall src tests
```

## Final Notes

Start small, validate often, and treat every profile change as a new system to test.
