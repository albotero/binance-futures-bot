import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text

from futures_bot.exchange import BinanceAPIError, BinancePayloadError
from futures_bot.exchange.leverage import LeverageLookupError, LeverageProvider
from futures_bot.persistence.database import make_engine, make_session_factory
from futures_bot.research import ResearchRequest, execute_research
from futures_bot.risk.configuration import (
    MissingRiskConfiguration,
    load_risk_configuration,
    verify_leverage_configuration,
)
from futures_bot.strategy.configuration import MissingStrategyConfiguration

from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings()
    engine = (
        make_engine(active_settings.database_url) if active_settings.dashboard_enabled else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if engine is not None:
            try:
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                if revision != "20261016_0006":
                    raise RuntimeError("database schema is not current; run alembic upgrade head")
                if not (Path(active_settings.frontend_directory) / "index.html").is_file():
                    raise RuntimeError("dashboard build is missing; build frontend before startup")
                application.state.database_ready = True
                yield
            finally:
                engine.dispose()
        else:
            yield

    app = FastAPI(
        title="Binance Futures Platform",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.database_ready = False

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str | bool]:
        return {
            "status": "alive",
            "mode": active_settings.mode,
            "exchange_environment": active_settings.exchange_environment,
            "database": "ready" if app.state.database_ready else "not_initialized",
            "live_trading_enabled": False,
        }

    @app.get("/ready", include_in_schema=False)
    def ready() -> dict[str, str | bool]:
        if not app.state.database_ready:
            raise HTTPException(503, "dashboard database is not initialized")
        return {"status": "ready", "live_trading_enabled": False, "trading_readiness": False}

    if engine is not None:
        from futures_bot.web import configure_web
        from futures_bot.web.auth import WebAuthSettings

        factory = make_session_factory(engine)
        research_slot = asyncio.Lock()
        leverage_provider = LeverageProvider(active_settings.binance_leverage_credentials_file)

        @app.post("/api/research/run")
        async def research_run(request: Request, payload: ResearchRequest) -> dict[str, object]:
            if research_slot.locked():
                raise HTTPException(409, "another research run is already executing")
            async with research_slot:
                with factory() as session:
                    try:
                        saved = load_risk_configuration(session)
                        verified_risk = await verify_leverage_configuration(
                            saved.configuration, leverage_provider
                        )
                        run_id, result = await execute_research(
                            session,
                            payload,
                            actor=request.state.owner_username,
                            risk_override=verified_risk,
                            expected_risk_version=saved.version,
                        )
                        session.commit()
                    except (MissingRiskConfiguration, MissingStrategyConfiguration) as exc:
                        raise HTTPException(409, str(exc)) from exc
                    except (BinanceAPIError, BinancePayloadError) as exc:
                        raise HTTPException(
                            502, "public exchange data is unavailable or invalid"
                        ) from exc
                    except LeverageLookupError as exc:
                        raise HTTPException(503, str(exc)) from None
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from exc
            return {"id": run_id, "result": result.model_dump(mode="json")}

        configure_web(
            app,
            factory,
            WebAuthSettings(
                allowed_origins=(active_settings.public_origin,),
                cookie_secure=active_settings.secure_cookies,
                local_mode=not active_settings.secure_cookies,
                session_ttl_seconds=active_settings.session_lifetime_seconds,
                frontend_dist=Path(active_settings.frontend_directory),
            ),
            leverage_provider=leverage_provider,
        )
    return app
