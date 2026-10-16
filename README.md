# Binance Futures Platform

A new, paper-first Binance USDⓈ-M Futures platform. This implementation is
isolated from the earlier project preserved on `archive/btcusdc-maker-scalper`.
Historical research, paper execution, and any future guarded live mode will use
shared strategy and risk components.

## Safety status

- An optional authenticated operator dashboard supports configuration,
  historical research, audits, and persistent single-symbol paper trading.
- Trading mode is restricted to `backtest` or `paper`.
- A public-data worker processes simulated closed-bar trades; it cannot
  submit exchange orders. Exchange API credentials are not required.
- The guarded live coordinator is an unconnected building block, not a
  usable live Binance adapter. There is no live activation route.
- `/health` reports liveness; `/ready` checks dashboard/database startup,
  not safe trading or exchange connectivity.
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

Run the initial liveness-only API on loopback:

```bash
python -m futures_bot
```

The service binds to `127.0.0.1:8000`. `GET /health` reports process liveness
and the selected non-secret mode/environment. API documentation routes remain
disabled. To enable the dashboard, follow the authenticated setup below.

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

Leverage is part of risk-controlled configuration, defaults to **1x**, and has
a platform ceiling of **50x**. The suggested dashboard policy remains **5x**;
existing saved limits are never raised automatically. The backend's omitted
policy-limit default remains 20x for compatibility. Set the portfolio limit
explicitly before selecting higher per-asset leverage. Leverage changes simulated
margin requirements, never the configured stop-loss budget. Higher leverage
increases liquidation risk. The 50x simulation ceiling is not confirmation of a
contract's actual Binance account/notional leverage bracket; live operation
would still need verified exchange limits and remains unavailable.

## Database schema

The current Alembic revisions define versioned non-secret configuration,
strategy snapshots, backtest runs, account snapshots, signals, orders, fills,
positions, trades, risk events, audit events, incidents, and a normalized
historical candle cache. Initialize a new database with:

```bash
alembic upgrade head
```

Dashboard startup requires the current migrated schema and a built frontend.
Migrations use `BOT_DATABASE_URL` and never run automatically on startup.
The schema also includes persisted owner authentication sessions/login
throttles and transactional paper-run checkpoints/events.

## Authenticated dashboard

The dashboard uses an original blue/mint candlestick icon in its header,
browser tabs and Apple home-screen shortcut. Source artwork is
[`frontend/public/icon.svg`](frontend/public/icon.svg); the same directory
contains the multi-size ICO fallback and 180-pixel touch icon. This is project
artwork, not Binance branding or an indication of affiliation.

Build the local React/TypeScript frontend and create the owner account:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
alembic upgrade head
python -m futures_bot.web.bootstrap \
  --database-url sqlite:///./data/futures_platform.sqlite3 --username owner
```

Bootstrap prompts for the password without echoing it. For unattended secure
provisioning, `--password-file` accepts an owner-only regular file; never put
the password on the command line or commit it. There is no public registration.

For **loopback-only development**, start with:

```bash
BOT_DASHBOARD_ENABLED=true \
BOT_PUBLIC_ORIGIN=http://127.0.0.1:8000 \
BOT_SECURE_COOKIES=false \
python -m futures_bot
```

Open `http://127.0.0.1:8000`. Remote deployments require HTTPS, secure cookies,
the exact `BOT_PUBLIC_ORIGIN`, and the least-privilege deployment configuration.
Insecure-cookie mode rejects remote origins. Passwords are scrypt-hashed;
opaque sessions are server-side, expiring, HttpOnly, SameSite-strict cookies,
with CSRF checks on mutations, Host/Origin enforcement, and persistent login
throttles. Secrets and exchange credentials are not sent to the browser.

The UI exposes USDC/USDT, per-symbol leverage, all risk and strategy settings,
optimistic configuration versions, audit events, historical research,
CSV/JSON results, and paper controls. Suggested settings on a fresh install
are **not saved or activated** until explicitly submitted. Research calls
use public production market data and the saved settings, with one request
at a time per application process and a 30-day range bound.

### Selecting assets

In **Settings → Risk & assets**, choose USDC (default) or USDT, then search
the contract directory by asset or symbol, for example `BTC`, `ETH`, or `SOL`.
Add verified perpetual contracts using readable **BTC / USDC** labels instead
of typing exchange symbols. Already-selected assets cannot be added twice.
Each new asset starts enabled at 1x. You can disable or remove selections
and set per-asset leverage before explicitly saving the form.

The directory uses public Binance metadata, includes only trading perpetual
contracts whose quote **and margin** match the chosen currency, and caches
metadata per environment for up to 60 seconds. Choose the public production
directory or testnet directory explicitly; this does not enable real-money
trading or change a paper run's data environment. Unavailable data is reported
with a retry control; no invented contracts or alternate quote fallback is used.
Inactive contracts may have zero exchange increments in their metadata, but
are never selectable and cannot produce executable trading rules. Trading
contracts still require positive increments; invalid metadata fails closed.
The authenticated endpoint is `GET /api/market/contracts` with optional
`quote_asset=USDC|USDT` and `environment=production|testnet`.

Switching quote currency requires confirmation and clears only the unsaved
asset selection so that actual contracts must be selected again. It does not
rename symbols into potentially nonexistent contracts, edit saved settings
until submission, or change existing paper-run snapshots. At least one asset
must be selected to save. Research and paper selectors list only enabled,
saved assets, with their configured leverage.

## Persistent paper operation

After reviewing/saving risk and strategy settings, create a paper run in the
dashboard. It starts paused and snapshots those settings; later settings edits
do not silently alter a running simulation. Select production **public market
data** for realistic USDC coverage; this never selects real-money execution.

Run the separate public-data worker against the same database:

```bash
python -m futures_bot.execution.worker
```

Only one simulated portfolio/symbol is active at a time. Pause blocks entries
while continuing protection simulation. Stop drains an open position through
its existing stop/target/time stop; it does not pretend to flatten at an
unobserved price. Closed-bar checkpoints, ledger records, and account state
commit atomically; repeated delivery/restarts cannot duplicate fills.

The worker recovers at most 24 hours of contiguous bars, rejects entry on
bars over 90 seconds late, and explicitly faults on gaps/outages. Acknowledging
a fault returns to paused, not running. Review its reason and service logs
before recovery. Protection is simulated only while valid data is available;
no orders protect real money. Funding, fees, slippage and liquidation retain
the research engine's conservative assumptions.

## Backup and deployment operations

See [deployment procedures](deploy/README.md) for service templates, TLS/LAN
reverse proxy configuration, explicit activation, backup verification and
restore/rollback prerequisites. The installer records a release environment
snapshot; transitive Python/build dependencies are not yet hash-locked.
Operational notes in this repository are not proof of a completed deployment.

SQLite backup uses the online backup API, integrity/foreign-key checks,
owner-only output and no-overwrite publication. Restore requires explicit
confirmation that both writers are stopped. Backups contain account/session
data and must be protected as private operational state.

## Public exchange data

The read-only USDⓈ-M adapter exposes public server time, exchange metadata,
symbol latest prices, mark prices and current funding data, klines, and
historical funding-rate history from the Binance Futures testnet or production
market-data API. It requires no API keys and has no order-submission methods.
Responses are validated and normalized to UTC timestamps and `Decimal` values;
requests use bounded concurrency, bounded retries, and sanitized errors.

Historical candles can be fetched in bounded pages, checked for gaps, and
stored in the SQLite cache. Offline backtests accept normalized candle and
funding series; API, dashboard, and paper-loop orchestration remain pending.
Symbol validation accepts Unicode letters used by current Binance listings,
while still rejecting separators, control characters, and lowercase symbols.
Unit tests use mocked HTTP responses and never call Binance.

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
fallback. The offline backtester uses this same risk engine. Exchange account
fetching, API controls, and paper-loop integration remain pending.

## Operator configuration

The web-based configuration screen and authenticated API store the required
risk policy and unique per-symbol settings under `risk` in the versioned
runtime configuration. Leverage defaults to 1x, must be an integer, and cannot
exceed the policy cap or the platform's 50x ceiling. Entry evaluation applies
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

Use the authenticated UI for routine settings; `.env` remains host bootstrap
configuration, not the trading control plane. All changes preserve unrelated
runtime fields and use the shared optimistic version/audit transaction.

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

Parameters are typed, serializable, versioned, and editable in the dashboard. Saving
operator strategy settings uses the shared runtime version and audit, while
retaining immutable parameter snapshots. Each generated setup carries its
implementation version and configuration checksum. Incomplete candles cannot
produce setups, indicator warm-up is required, and missing historical bars
raise explicit errors. This is a research baseline, not evidence of a trading
edge, and is not yet connected to an automated execution loop.

## Offline historical research

`futures_bot.backtesting.run_backtest` simulates one enabled symbol with at
most one pending setup and one open position. It uses the shared closed-bar
strategy, trigger/exit rules, saved leverage, and central risk checks.
Provide complete ordered 1h/15m histories with warm-up, historical funding,
exchange rules, explicit risk limits, and exact-decimal simulation assumptions.
The function makes no network requests. Missing or inconsistent histories
raise errors; denied entries include risk rejection reasons.

Results include a dataset checksum, engine/strategy versions, configuration
snapshot, trade ledger, equity curve, net fees/funding, drawdown, win rate,
profit factor, and expectancy. JSON serialization preserves decimal strings;
`backtesting.results` provides audited transactional result storage and CSV
trade export. Results are not automatically promoted into trading settings.

Important limitations: execution timestamps are bar bounds, price slippage
is adverse, stop/target ambiguity is stop-first, and uncertain intrabar
funding charges debits but omits credits. Liquidation is a conservative
adverse-extreme stress test using explicit maintenance/fee assumptions, not
Binance bracket-accurate liquidation. Exchange rules are static over a run.
An empty funding series explicitly models zero funding, not verified coverage.
Gaps, costs, and slippage can exceed gross risk estimates or invalidate the
required reward-to-risk ratio. Multi-symbol allocation and dashboard
orchestration are not implemented yet.

### Chronological validation and parameter search

`backtesting.validation.run_walk_forward_search` evaluates the baseline and
an explicit, deterministic set of candidate strategy configurations.
`ValidationPlan` defines ordered training/validation folds and a final
holdout, with purge gaps covering the longest candidate entry-plus-exit
horizon. Later candles and funding do not reach earlier backtests.
Previously closed bars may warm indicators; trading does not occur in purge
gaps. Each window starts from the same simulated capital with no positions.

Search is deliberately bounded: at most 16 strategies including the baseline,
12 folds, and a configurable budget capped at 128 backtests. Risk limits,
quote asset, leverage, and cost assumptions remain fixed across trials.
Default eligibility requires at least 20 training and 10 validation trades
per fold, drawdown no greater than 20%, no halted simulation, and positive
validation return in every fold. These research gates do not initialize or
replace operator risk settings.

Eligible candidates rank by their worst validation return minus drawdown
penalty, then mean score, with deterministic checksum tie-breaking. Selection
is frozen before the holdout is evaluated. If no candidate qualifies, the
report explicitly selects none; the baseline holdout is still reported.
All trials, rejection reasons, scores, dataset checksums, assumptions, and
holdout results serialize to JSON and can be stored transactionally through
`backtesting.results.save_validation_result`, with an audit event and no
runtime-configuration changes.

This is parameter research, **not a trained predictive ML model** or proof of
profitability. Validation periods guide selection; only the final holdout is
excluded from selection within a run. Repeatedly revising candidates after
viewing that holdout contaminates it. Fold returns are not a compounded
portfolio, and a failed holdout must not be hidden or used for automatic
promotion. No model training, dashboard search controls, or trading
activation is introduced.

## Roadmap

The implementation phases, acceptance criteria, architecture map, deployment
preflight, and test journals are maintained in the Git-ignored `.local/`
directory on the development workstation. The application can be hosted for
authenticated paper research, but is not ready for live trading.
