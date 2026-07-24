# Futures Bot

Binance USDT/USDC-margined futures trading bot with live trading, paper trading, local dashboards, cached backtests, and SQLite trade history.

This project is tooling, not financial advice.

## What It Does

- Runs live or paper futures trades from strategy profiles in `data/strategies`
- Places live TP/SL protection with Binance algo orders when enabled
- Reconciles exchange state back into the local DB
- Backfills stored trade history from Binance user-trade fills for more accurate entry/exit prices
- Caches backtest candle history locally so repeated parameter tests are much faster

## Safety

Before live trading:

1. Run paper mode first.
2. Warm candle cache and backtest repeatedly.
3. Use Binance testnet before mainnet if possible.
4. Keep leverage and risk per trade conservative until the profile is stable.

Mainnet live mode requires:

- `BOT_MODE=live`
- valid `BINANCE_API_KEY`
- valid `BINANCE_API_SECRET`
- `BOT_LIVE_TRADING_CONFIRMED=true`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before running live commands.

## Run As A systemd Service

This project includes a systemd template unit at `deploy/systemd/futures-bot@.service`.

Use this when you want the bot managed by `systemctl` with auto-restart and boot startup.

1. Copy and edit the unit file:

```bash
sudo cp deploy/systemd/futures-bot@.service /etc/systemd/system/
sudo nano /etc/systemd/system/futures-bot@.service
```

Update these fields in the copied unit:

- `User=`
- `Group=`
- `WorkingDirectory=`
- `Environment=PYTHONPATH=`
- `ExecStart=`
- `ReadWritePaths=`

The bot loads `.env` automatically from `WorkingDirectory` via `python-dotenv`, so no `EnvironmentFile=` line is required.
The template uses `/usr/bin/bash -lc` in `ExecStart=` for better systemd compatibility when launching the virtualenv interpreter.

2. Reload systemd and enable one mode:

```bash
sudo systemctl daemon-reload

# Engine only
# sudo systemctl enable --now futures-bot@run-bot.service

# Or dashboard + API
sudo systemctl enable --now futures-bot@run-web.service
```

3. Check status and logs:

```bash
sudo systemctl status futures-bot@run-web.service
journalctl -u futures-bot@run-web.service -f
```

4. Common operations:

```bash
sudo systemctl restart futures-bot@run-web.service
sudo systemctl stop futures-bot@run-web.service
sudo systemctl disable futures-bot@run-web.service
```

## Run As An OpenRC Service (Alpine Linux)

Alpine Linux uses OpenRC by default. This project includes OpenRC init and config files at:

- `deploy/openrc/futures-bot.initd` — init script
- `deploy/openrc/futures-bot.confd` — configuration
- `deploy/openrc/futures-bot-wrapper.sh` — environment wrapper

**Installation on Alpine:**

1. Copy all files to the system:

```bash
sudo cp deploy/openrc/futures-bot.initd /etc/init.d/futures-bot
sudo cp deploy/openrc/futures-bot.confd /etc/conf.d/futures-bot
sudo cp deploy/openrc/futures-bot-wrapper.sh /usr/local/bin/futures-bot-wrapper.sh
sudo chmod +x /etc/init.d/futures-bot /usr/local/bin/futures-bot-wrapper.sh
```

2. Edit the config file with your values:

```bash
sudo nano /etc/conf.d/futures-bot
```

Update:

- `BOT_USER=` — Linux user to run the bot
- `BOT_GROUP=` — Linux group to run the bot
- `BOT_DIR=` — Path to the bot working directory
- `BOT_VENV=` — Path to the virtual environment
- `BOT_MODE=` — `run-web` for dashboard+API, or `run-bot` for engine only
- `BOT_WRAPPER=` — Path to wrapper script (usually `/usr/local/bin/futures-bot-wrapper.sh` or `$BOT_DIR/deploy/openrc/futures-bot-wrapper.sh`)

3. Enable and start the service:

```bash
sudo rc-service futures-bot start
sudo rc-update add futures-bot
```

4. Verify and manage:

```bash
sudo rc-service futures-bot status
sudo rc-service futures-bot restart
sudo rc-service futures-bot stop
```

View logs:

```bash
tail -f /var/log/futures-bot.log
```

## Main Commands

Run dashboard (set PYTHONPATH first):

```bash
export PYTHONPATH=$PWD/src
futures-bot run-web

# Or run directly:
.venv/bin/python -m futures_bot.main run-web
```

Run engine only:

```bash
export PYTHONPATH=$PWD/src
futures-bot run-bot

# Or run directly:
.venv/bin/python -m futures_bot.main run-bot
```

Seed default strategy:

```bash
futures-bot seed-strategy
```

Run backtest:

```bash
futures-bot backtest --profile ema7_20_trend_strict --symbol BTCUSDC --symbol ETHUSDC
```

Warm 12-week candle cache once:

```bash
futures-bot warm-backtest-cache --duration 12w
```

Backfill local trade history from Binance fills:

```bash
futures-bot sync-exchange-history
```

## Backtest Workflow

Recommended loop:

1. Warm cache once with `warm-backtest-cache --duration 12w`
2. Tune profile JSON and `.env`
3. Re-run `futures-bot backtest ...`
4. Compare reports in `data/reports`

Cached candles are stored under `data/cache/backtest_candles` by default.

## Live Price Accuracy

For live mode:

- entries use Binance order fills, not local candle closes
- closes use Binance order fills, not local chart prices
- external/manual exchange closes are reconciled from Binance user-trade history when available
- `sync-exchange-history` can backfill historical rows in `data/bot.db`

## Important Environment Variables

Core:

- `BOT_MODE`
- `BOT_SYMBOLS`
- `BOT_TRADE_ALL_SYMBOLS`
- `BOT_QUOTE_ASSET`
- `BOT_STRATEGY_PROFILE`
- `BOT_INTERVAL`
- `BOT_CANDLES_LIMIT`
- `BOT_INITIAL_EQUITY`

Risk and execution:

- `BOT_RISK_PER_TRADE_PCT`
- `BOT_RISK_REWARD_RATIO`
- `BOT_LEVERAGE`
- `BOT_MAX_LEVERAGE`
- `BOT_MAX_OPEN_POSITIONS`
- `BOT_MAX_POSITION_PCT`
- `BOT_STOP_LOSS_PCT`
- `BOT_TAKE_PROFIT_PCT`
- `BOT_TRAILING_STOP_PCT`

Two-stage trailing:

- `BOT_TRAILING_STAGE_ENABLED`
- `BOT_TRAILING_BREAK_EVEN_R`
- `BOT_TRAILING_ACTIVATION_R`
- `BOT_TRAILING_FEE_BUFFER_PCT`

Backtest speed:

- `BOT_BACKTEST_DURATION`
- `BOT_BACKTEST_MAX_CANDLES`
- `BOT_BACKTEST_EVAL_WINDOW`
- `BOT_BACKTEST_CACHE_ENABLED`
- `BOT_BACKTEST_CACHE_TTL_HOURS`
- `BOT_BACKTEST_CACHE_DIR`

Live safety:

- `BOT_LIVE_TRADING_CONFIRMED`
- `BINANCE_TESTNET`
- `BINANCE_BASE_URL`
- `BINANCE_TESTNET_URL`
- `BOT_LIVE_PROTECTION_MODE`

Storage:

- `BOT_DATA_DIR`
- `BOT_DB_PATH`

## Strategy Files

Profiles live in `data/strategies/*.json`.

Each rule uses one of:

- `ema_cross`
- `macd`
- `rsi`
- `bollinger`
- `adx`

Each profile sets:

- `threshold`
- `description`
- weighted `rules`

## API Endpoints

Main API endpoints:

- `GET /api/status`
- `GET /api/trades`
- `GET /api/history`
- `GET /api/exchange`
- `POST /api/start`
- `POST /api/stop`
- `POST /api/pause`
- `POST /api/resume`
- `POST /api/run-once`
- `POST /api/trades/{symbol}/close`
- `GET /api/strategies`
- `POST /api/strategies/save`
- `POST /api/strategies/load/{name}`
- `POST /api/backtest/run`

## Files You Will Use Most

- `data/strategies/` for profile tuning
- `data/reports/` for backtest outputs
- `data/cache/backtest_candles/` for cached candle history
- `data/bot.db` for persisted trade and snapshot history

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
