# Futures Bot

Binance USDT/USDC-margined futures trading bot with live trading, paper trading, local dashboards, cached backtests, and SQLite trade history.

This project is tooling, not financial advice.

## What It Does

- Runs live or paper futures trades from strategy profiles in `data/strategies`
- Places live TP/SL protection with Binance algo orders when enabled
- Promotes live protection automatically from fixed SL/TP to break-even and exchange trailing when hybrid mode is enabled
- Reconciles exchange state back into the local DB
- Backfills stored trade history from Binance user-trade fills for more accurate entry/exit prices
- Caches backtest candle history locally so repeated parameter tests are much faster
- Checks authenticated order creation and cancellation from the CLI or dashboard

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

**Installation on Alpine:**

1. Copy the init script and config:

```bash
sudo cp deploy/openrc/futures-bot.initd /etc/init.d/futures-bot
sudo cp deploy/openrc/futures-bot.confd /etc/conf.d/futures-bot
sudo chmod +x /etc/init.d/futures-bot
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
- `BOT_COMMAND=` — `run-web` for dashboard+API, or `run-bot` for engine only

Do not set `BOT_MODE` in `/etc/conf.d/futures-bot`. Trading mode belongs in the
project `.env` as `BOT_MODE=paper` or `BOT_MODE=live`; exporting it from OpenRC
would override the `.env` value.

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

If the dashboard reports `<urlopen error [Errno -3] Try again>`, Alpine cannot
resolve the Binance hostname. This happens before strategy evaluation or order
submission. Verify DNS and HTTPS from the same host:

```bash
cat /etc/resolv.conf
getent hosts fapi.binance.com
wget -S -O - https://fapi.binance.com/fapi/v1/ping
```

If hostname lookup fails, repair the Alpine host's DNS configuration (DHCP,
network manager, container DNS, or the nameservers that generate
`/etc/resolv.conf`) before restarting the bot. Recopy the updated OpenRC script
so startup waits for Alpine's `net` service:

```bash
sudo cp deploy/openrc/futures-bot.initd /etc/init.d/futures-bot
sudo chmod +x /etc/init.d/futures-bot
sudo rc-service futures-bot restart
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

Check order creation and cancellation for the first pair in `BOT_SYMBOLS`:

```bash
futures-bot test-order

# Or run directly:
PYTHONPATH=src .venv/bin/python -m futures_bot.main test-order
```

## Dashboard

The dashboard runs with `run-web` and is served at
`http://127.0.0.1:8010` by default. It provides:

- engine start, stop, pause, resume, and one-cycle controls
- live account metrics, open positions, signals, and recent trades
- a line-only equity and PnL chart built from stored snapshots
- strategy profile selection and backtest controls
- exchange connectivity and order checks

Selecting **Start** asks whether to clear previous trade and chart history.
Keeping history starts normally. Clearing history deletes stored trades and
snapshots, resets local realized PnL and equity history, and is refused while
the bot or an open position is active.

Runtime error banners can be dismissed in the current browser. A different
error appears normally even after the previous message was dismissed.

### Estimated Liquidation Proximity

The **Est. Liq. Proximity** metric is not the distance to stop loss. It estimates
how far the worst open position has moved adversely from entry toward a simple
leverage-based liquidation boundary:

```text
estimated liquidation distance = entry price / leverage
proximity = adverse move from entry / estimated liquidation distance * 100
```

The value is clamped from `0%` to `100%`. Entry and favorable movement display
`0%`; halfway toward the estimated boundary displays `50%`.

This is an operational estimate, not Binance's exact liquidation price. Actual
liquidation depends on maintenance margin tiers, fees, isolated or cross margin,
wallet balance, and other open positions. Confirm critical risk using the
liquidation price and margin ratio shown by Binance.

## Exchange Order Check

The order check verifies authenticated order creation and cancellation against
the configured Binance Futures environment. It always uses the first pair in
the comma-separated `BOT_SYMBOLS` list. For example, this selects `BTCUSDC`:

```dotenv
BOT_SYMBOLS=BTCUSDC,ETHUSDC,DOGEUSDC
```

The check:

1. Loads the current ticker and exchange filters for the selected pair.
2. Calculates a quantity that satisfies Binance lot-size and minimum-notional rules.
3. Places a small post-only (`GTX`) limit buy below the current market.
4. Immediately cancels the returned order ID.
5. Reports the environment, symbol, order ID, and create/cancel statuses.

Run it from any of these surfaces:

- CLI: `futures-bot test-order`
- Dashboard: select **Check Order** in the top control bar and confirm the prompt
- API: `POST /api/test-order`

Example API request:

```bash
curl -X POST http://127.0.0.1:8010/api/test-order
```

Use testnet whenever possible:

```dotenv
BINANCE_TESTNET=true
BINANCE_API_KEY=your_testnet_key
BINANCE_API_SECRET=your_testnet_secret
```

Important safety details:

- This is a real exchange order check, even when `BOT_MODE=paper`.
- Mainnet checks are refused unless `BOT_LIVE_TRADING_CONFIRMED=true`.
- A post-only order cannot execute immediately as a taker, but any accepted
  resting order can theoretically fill before its cancellation is processed.
- The command fails if `BOT_SYMBOLS` is empty, credentials are missing, or the
  first configured pair is unavailable in the selected environment.

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

## TP, SL, And Trailing Modes

### Initial SL And TP Levels

When a strategy produces an exit plan, the bot derives the initial stop loss
from recent swing levels and average candle range. Take profit is then derived
from that risk distance and `BOT_RISK_REWARD_RATIO`. Strategy-derived levels
take precedence over the fallback percentages.

When no strategy exit plan is available:

- `BOT_STOP_LOSS_PCT` sets the initial stop distance from entry.
- `BOT_RISK_REWARD_RATIO` sets take profit as a multiple of that stop distance.
- `BOT_TAKE_PROFIT_PCT` is currently loaded for compatibility but is not used
  to price live or backtest take-profit orders.

The bot sizes quantity from the initial stop distance and
`BOT_RISK_PER_TRADE_PCT`. Widening a stop normally reduces quantity rather than
increasing the configured account risk, subject to exchange fills, fees,
slippage, and position-size caps.

### Protection Location

`BOT_LIVE_PROTECTION_MODE` controls where live exits are enforced:

| Value                | Behavior                                                                                                                                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `local_only`         | The bot loop checks SL, TP, and trailing levels and submits a market close. No protective algo orders are placed on Binance. Loss of the bot process or connectivity leaves no exchange-side bot protection. |
| `local_and_exchange` | Binance receives reduce-only `STOP_MARKET` and `TAKE_PROFIT_MARKET` algo orders at entry. Trailing behavior depends on the mode below. This is required for live hybrid trailing.                            |

Paper mode always simulates protection locally.

### Trailing Mode Matrix

| Staged  | Hybrid  | Live behavior with `local_and_exchange`                                                                                                                                                                                                |
| ------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `false` | `false` | Legacy immediate trailing. Fixed SL, fixed TP, and an exchange trailing order are placed at entry. The fixed TP still caps profit.                                                                                                     |
| `true`  | `false` | Staged local trailing. Binance keeps the original fixed SL/TP. The bot moves its local stop to break-even and activates local trailing at the configured R thresholds.                                                                 |
| `true`  | `true`  | Automatic hybrid trailing. Binance starts with fixed SL/TP, receives a replacement break-even SL, then receives an exchange trailing order. The fixed TP is removed after trailing placement succeeds so winners can continue running. |
| `false` | `true`  | Invalid. Startup fails because hybrid mode requires staged trailing.                                                                                                                                                                   |

Configure automatic hybrid mode with:

```dotenv
BOT_LIVE_PROTECTION_MODE=local_and_exchange
BOT_TRAILING_STAGE_ENABLED=true
BOT_HYBRID_TRAILING_ENABLED=true
BOT_TRAILING_BREAK_EVEN_R=0.8
BOT_TRAILING_ACTIVATION_R=1.2
BOT_TRAILING_STOP_PCT=1.6
BOT_TRAILING_FEE_BUFFER_PCT=0.04
```

`R` is the absolute distance between entry and the original stop loss. For
example, with entry `100` and initial stop `99`, `1R` is a favorable move of
`1`. At `0.8R`, the example long reaches `100.8` and becomes eligible for
break-even promotion. At `1.2R`, it reaches `101.2` and becomes eligible for
trailing activation.

For a long, break-even is entry plus `BOT_TRAILING_FEE_BUFFER_PCT`; for a short,
it is entry minus that buffer. The buffer is a price percentage intended to
offset fees and slippage. It does not guarantee positive net PnL.

`BOT_TRAILING_STOP_PCT` becomes the Binance trailing callback rate in hybrid
mode and is clamped to Binance's supported `0.1%` to `5.0%` range. Protective
triggers use Binance mark price.

### Hybrid Order Sequence

Automatic hybrid mode uses replacement-first sequencing:

1. Entry fill is confirmed.
2. Binance fixed SL and TP orders are placed.
3. At the break-even threshold, a replacement SL is placed before the original
   SL is canceled.
4. At trailing activation, the Binance trailing order is placed before the
   fixed TP is canceled.
5. Local fixed-TP enforcement is disabled only for the trailing phase.
6. A temporary TP cancellation failure is retried on later polling cycles
   without creating another trailing order.

If initial exchange SL/TP placement fails, the live executor attempts an
emergency market flatten. Do not intentionally restart the current bot version
with an open staged position: staged flags and protective-order roles are held
in process memory. Close or otherwise manage the position first, then restart.

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
- `BOT_HYBRID_TRAILING_ENABLED`
- `BOT_TRAILING_BREAK_EVEN_R`
- `BOT_TRAILING_ACTIVATION_R`
- `BOT_TRAILING_FEE_BUFFER_PCT`

See [TP, SL, And Trailing Modes](#tp-sl-and-trailing-modes) for the mode matrix
and live exchange order lifecycle.

Backtest speed:

- `BOT_BACKTEST_DURATION`
- `BOT_BACKTEST_MAX_CANDLES`
- `BOT_BACKTEST_EVAL_WINDOW`
- `BOT_BACKTEST_CACHE_ENABLED`
- `BOT_BACKTEST_CACHE_TTL_HOURS`
- `BOT_BACKTEST_CACHE_DIR`

Live safety:

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
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
- `POST /api/start` with optional JSON body `{"clear_history": true}`
- `POST /api/stop`
- `POST /api/pause`
- `POST /api/resume`
- `POST /api/run-once`
- `POST /api/test-order`
- `GET /api/config`
- `POST /api/seed-default-strategy`
- `POST /api/trades/{symbol}/close`
- `GET /api/strategies`
- `POST /api/strategies/save`
- `POST /api/strategies/load/{name}`
- `POST /api/backtest/run`
- `GET /api/backtest/jobs/{job_id}`
- `POST /api/backtest/jobs/{job_id}/cancel`

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

- adaptive_trend_guarded
- ema30_60_trend_strict
- ema7_20_adx_balanced
- ema7_20_trend_strict
- mean_reversion_range
- trend_aggressive_breakout
- trend_balanced_multi
- trend_conservative_multi

Use one by setting `BOT_STRATEGY_PROFILE` to the profile name.

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
.venv/bin/python -m unittest tests.test_trading_bot
```

Compile check:

```bash
.venv/bin/python -m compileall src tests
node --check src/futures_bot/dashboard/static/app.js
```

## Final Notes

Start small, validate often, and treat every profile change as a new system to test.
