from fastapi import FastAPI

from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings()
    app = FastAPI(
        title="Binance Futures Platform",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = active_settings

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str | bool]:
        return {
            "status": "alive",
            "mode": active_settings.mode,
            "exchange_environment": active_settings.exchange_environment,
            "database": "not_initialized",
            "live_trading_enabled": False,
        }

    return app
