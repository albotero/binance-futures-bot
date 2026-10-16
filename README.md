# Binance Futures Platform

A new, paper-first Binance USDⓈ-M Futures platform. This implementation is
isolated from the earlier project preserved on `archive/btcusdc-maker-scalper`.
Historical research, paper execution, and any future guarded live mode will use
shared strategy and risk components.

## Safety status

- The initial service exposes only a read-only liveness endpoint.
- Trading mode is restricted to `backtest` or `paper`.
- Live execution, exchange credentials, authentication, and automated trading
  loops are not implemented yet.
- Do not run this foundation as a trading bot or treat `/health` as readiness.
- No profitability or deployment claims are made.

## Futures, cryptocurrency, and liability disclaimer

Trading cryptocurrency futures is highly speculative and can result in rapid,
substantial, or total loss. Leverage magnifies exposure and can trigger
liquidation; depending on the product, exchange rules, and applicable law,
losses may exceed the collateral committed. Crypto markets can be volatile,
illiquid, interrupted, manipulated, or unavailable. Exchange failures, bad
market data, network outages, software defects, configuration mistakes, and
security incidents can also cause financial loss. Backtests and simulated
results do not predict future performance.

This project is provided **as-is for educational and research purposes**. It
is not financial, investment, legal, or tax advice, and it is not an offer,
solicitation, or recommendation to trade. No result, uptime, security, or
profitability is guaranteed. The software is incomplete; do not use it to
place live orders.

You are solely responsible for evaluating, configuring, securing, and using
this software, for your trading and compliance decisions, and for any use by
people to whom you provide or deploy it. To the fullest extent permitted by
applicable law, the author, contributors, and maintainers disclaim
responsibility and liability for losses, claims, or damages arising from this
software or its use by you or anyone else. This notice does not exclude
liability or rights that cannot legally be excluded or waived.

## Development setup

Python 3.12 is the minimum supported runtime. The foundation is also being
validated on Python 3.14.8. Create an isolated environment and install the
pinned project dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
pytest
ruff check .
```

Run the local API on loopback only:

```bash
python -m futures_bot
```

The service binds to `127.0.0.1:8000`. `GET /health` reports process liveness
and the selected non-secret mode/environment; it does not prove database or
exchange readiness. API documentation routes are disabled until authentication
is implemented.

## Configuration

The example file contains only bootstrap values. The initial safe defaults are
paper mode, Binance testnet market data, and a dedicated database path
`data/futures_platform.sqlite3`. **USDC is the default quote and account
denomination**; USDT is available only as an explicit operator selection.
Only trading perpetual contracts whose exchange-reported quote and margin
assets both match the selected asset are eligible. There is no automatic
USDT fallback when a USDC symbol or account is unavailable.
The intended operator workflow is to configure
symbols, strategy, risk limits, and per-symbol leverage in the authenticated
web interface; these values are versioned and audited in the database rather
than edited in `.env`. `.env` is reserved for host-specific bootstrap settings
such as the database location and, only if needed in a later phase, secret
injection. Exchange credentials are never exposed to the browser or saved as
ordinary strategy configuration.

Leverage is part of risk-controlled configuration, defaults to **1x**, and is
planned to have a conservative platform cap of **20x** until separately
reviewed. Leverage changes simulated margin requirements; it must never
increase the position's configured loss-risk budget.

## Database schema

The current Alembic revisions define versioned non-secret configuration,
strategy snapshots, backtest runs, account snapshots, signals, orders, fills,
positions, trades, risk events, audit events, incidents, and a normalized
historical candle cache. Initialize a new database with:

```bash
alembic upgrade head
```

The service does not yet connect to this schema; persistence integration is a
later milestone. Migrations use the configured `BOT_DATABASE_URL` and do not
automatically create or upgrade a database on application startup.

## Public exchange data

The read-only USDⓈ-M adapter exposes public server time, exchange metadata,
symbol latest prices, mark prices and current funding data, klines, and
historical funding-rate history from the Binance Futures testnet or production
market-data API. It requires no API keys and has no order-submission methods.
Responses are validated and normalized to UTC timestamps and `Decimal` values;
requests use bounded concurrency, bounded retries, and sanitized errors.

Historical candles can be fetched in bounded pages, checked for gaps, and
stored in the SQLite cache. The data services are not yet orchestrated by the
API, dashboard, backtester, or paper-execution loop. Unit tests use mocked HTTP
responses and never call Binance.

## Risk-sizing status

A standalone Decimal-based sizing module evaluates stop-based risk budgets,
exchange price/quantity filters, account and market-data freshness, portfolio
limits, protective-order/connectivity health, margin reserve, and the
platform/exchange leverage ceilings. Quantity is rounded down to exchange
increments, and leverage changes required margin only; it does not increase
the risk budget. Estimated stop loss is a gross price-distance estimate and
does not guarantee realized loss under slippage, gaps, fees, or liquidation.
The configured entry evaluator loads versioned risk settings and the latest
normalized account snapshot for the explicitly selected environment. It
returns the configuration version, snapshot ID, applied leverage, and risk
decision. Missing/invalid settings or missing account state fail explicitly;
stale snapshots reject the entry. Account snapshots are stored independently
for simulated, testnet, and production environments, with no cross-environment
fallback. Exchange account fetching, strategy, API, backtester, and paper-loop
integration are still pending.

## Operator configuration

The web-based configuration screen and its authenticated API are planned and
not yet implemented. The typed backend risk configuration stores the required
risk policy and unique per-symbol settings under `risk` in the versioned
runtime configuration. Leverage defaults to 1x, must be an integer, and cannot
exceed the policy cap or the platform's 20x ceiling. Entry evaluation applies
the saved leverage, not a caller's requested override, and still checks the
exchange limit.

The risk document has a `quote_asset` selector (`"USDC"` by default or
`"USDT"`). Every configured symbol must match that quote; use `BTCUSDC`, for
example, for USDC. Saved account snapshots are isolated by both environment
and denomination. Exchange metadata is checked during risk evaluation rather
than trusting a symbol suffix alone. Switching quote requires matching symbols
and a fresh matching account snapshot; USDC and USDT balances are never added
or converted implicitly.

The backend configuration repository requires the expected runtime version
and saves its audit event in the same transaction. It preserves unrelated
runtime fields and rejects stale updates. Money and fractional values are
serialized as exact decimal strings (for example, `"0.01"` means 1% risk);
JSON floating-point policy inputs are rejected. Durations serialize as
ISO 8601 durations such as `"PT15S"`. Risk budgets must be explicitly
configured; loading missing settings does not create default trading limits.

Do not treat editing `.env` as the supported operator UI. The service still
has no authentication or state-changing configuration endpoint; these backend
helpers are preparation for the authenticated dashboard, not exposed controls.

When upgrading an older installation, run `alembic upgrade head`. Account
snapshots without a recorded denomination are retained as `UNKNOWN` and
cannot be used for either USDC or USDT sizing; capture new normalized snapshots.
Old USDT risk configurations must explicitly set `"quote_asset": "USDT"`
before use, or be replaced with a USDC configuration and matching symbols.

## Strategy baseline

The pure `ema-pullback-v1` strategy uses closed 1-hour EMA50/EMA200 trend
alignment and a directional closed 15-minute candle that touches EMA20 and
closes back on the trend side. A setup triggers one exchange tick beyond the
signal candle for the next three bars. Its stop uses the wider of the five-bar
swing plus one tick or 1.5 times Wilder ATR14; the target is at least 2R after
tick rounding. Unfilled setups invalidate at the stop or expire after three
bars; positions have an eight-bar time stop. Bar ambiguity resolves stop-first,
and adverse opening gaps are not filled at a fictitious trigger/stop price.

Parameters are typed, serializable, and versioned for future web forms. Saving
operator strategy settings uses the shared runtime version and audit, while
retaining immutable parameter snapshots. Each generated setup carries its
implementation version and configuration checksum. Incomplete candles cannot
produce setups, indicator warm-up is required, and missing historical bars
raise explicit errors. This is a research baseline, not evidence of a trading
edge, and is not yet connected to an automated execution loop.

## Roadmap

The implementation phases, acceptance criteria, architecture map, deployment
preflight, and test journals are maintained in the Git-ignored `.local/`
directory on the development workstation. The application is not ready for
live trading or production deployment.
