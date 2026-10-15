# Binance Futures Platform

A new, paper-first Binance USDⓈ-M Futures platform. This implementation is
isolated from the earlier project preserved on `archive/btcusdc-maker-scalper`.
Historical research, paper execution, and any future guarded live mode will use
shared strategy and risk components.

## Safety status

- The initial service exposes only a read-only liveness endpoint.
- Trading mode is restricted to `backtest` or `paper`.
- Live execution, exchange credentials, authentication, and database-backed
  state are not implemented yet.
- Do not run this foundation as a trading bot or treat `/health` as readiness.
- No profitability or deployment claims are made.

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

The example file contains only non-secret defaults. Runtime settings use the
`BOT_` prefix. The initial default is paper mode on the testnet environment,
with a dedicated database path `data/futures_platform.sqlite3`. Never reuse the
legacy project's database or copy credentials into Git, the browser, or
documentation.

## Roadmap

The implementation phases, acceptance criteria, architecture map, deployment
preflight, and test journals are maintained in the Git-ignored `.local/`
directory on the development workstation. The application is not ready for
live trading or production deployment.
