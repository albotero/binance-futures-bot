import csv
import hmac
import io
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from futures_bot.backtesting.engine import BacktestConfiguration
from futures_bot.execution.paper import (
    PaperEvent,
    PaperRun,
    control_paper_run,
    create_paper_run,
)
from futures_bot.persistence.models import (
    ApplicationConfiguration,
    AuditEvent,
    BacktestRun,
    utc_now,
)
from futures_bot.persistence.repositories import ConfigurationConflict
from futures_bot.risk.configuration import (
    MissingRiskConfiguration,
    RiskConfiguration,
    save_risk_configuration,
)
from futures_bot.strategy.configuration import (
    MissingStrategyConfiguration,
    save_strategy_configuration,
)
from futures_bot.strategy.pullback import StrategyConfiguration
from futures_bot.web.auth import WebAuthSettings, hash_password, token_hash, verify_password
from futures_bot.web.models import WebLoginThrottle, WebOwner, WebSession

COOKIE_NAME = "futures_session"
_DUMMY_HASH = hash_password("unused-dummy-login-credential")


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=0)
    configuration: dict[str, Any]


class PaperCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=32)
    simulation: BacktestConfiguration
    environment: Literal["testnet", "production"] = "testnet"
    start: AwareDatetime


class PaperControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=1)
    action: Literal["resume", "pause", "stop", "recover"]


def _errors(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if not any(
                fragment in "".join(c for c in key.lower() if c.isalnum())
                for fragment in ("secret", "password", "token", "apikey", "signature", "privatekey")
            )
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _csv(rows: list[dict[str, Any]], fields: list[str]) -> Response:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            text = str(value) if value is not None else ""
            safe[key] = "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        writer.writerow(safe)
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="research.csv"'},
    )


def configure_web(
    app: FastAPI, session_factory: sessionmaker[Session], settings: WebAuthSettings
) -> None:
    """Register routes after migrations, before serving; never creates database tables."""
    app.openapi_url = None
    app.docs_url = None
    app.redoc_url = None
    # Remove documentation routes even when the caller used FastAPI's defaults.
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if getattr(route, "path", "")
        not in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
    ]
    allowed_hosts = {urlsplit(origin).netloc.lower() for origin in settings.allowed_origins}

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _errors(exc)}, status_code=422)

    @app.middleware("http")
    async def operator_security(request: Request, call_next: Any) -> Response:
        def denied(status: int, message: str) -> JSONResponse:
            return JSONResponse({"detail": message}, status_code=status)

        if request.headers.get("host", "").lower() not in allowed_hosts:
            return denied(400, "untrusted host")
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            return denied(403, "untrusted origin")
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        if mutation and origin not in settings.allowed_origins:
            return denied(403, "same-origin Origin header required")
        public_document_navigation = (
            request.method in {"GET", "HEAD"}
            and request.url.path == "/"
            and request.headers.get("sec-fetch-mode") == "navigate"
            and request.headers.get("sec-fetch-dest") == "document"
        )
        if request.headers.get("sec-fetch-site") == "cross-site" and not public_document_navigation:
            return denied(403, "cross-site request refused")
        if request.url.path.startswith("/api/"):
            if mutation:
                if request.headers.get("content-length"):
                    try:
                        if int(request.headers["content-length"]) > 65536:
                            return denied(413, "request too large")
                    except ValueError:
                        return denied(400, "invalid content length")
                if len(await request.body()) > 65536:
                    return denied(413, "request too large")
            raw = request.cookies.get(COOKIE_NAME, "")
            key = token_hash(raw)
            with session_factory() as session:
                login_session = session.get(WebSession, key) if raw else None
                valid = login_session is not None and login_session.expires_at > utc_now()
                owner = (
                    session.get(WebOwner, login_session.owner_id)
                    if valid and login_session is not None and login_session.owner_id is not None
                    else None
                )
                if (
                    request.url.path not in {"/api/auth/session", "/api/auth/login"}
                    and owner is None
                ):
                    return denied(401, "authentication required")
                if mutation and (
                    not valid
                    or login_session is None
                    or not hmac.compare_digest(
                        request.headers.get("x-csrf-token", "").encode(),
                        login_session.csrf_token.encode(),
                    )
                ):
                    return denied(403, "valid CSRF token required")
                request.state.owner_username = owner.username if owner else None
                request.state.session_key = key if valid else None
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'; object-src 'none'"
                ),
            }
        )
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def issue_session(response: Response, owner_id: int | None) -> dict[str, Any]:
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        lifetime = settings.session_ttl_seconds if owner_id else 600
        expires = utc_now() + timedelta(seconds=lifetime)
        with session_factory.begin() as session:
            session.execute(delete(WebSession).where(WebSession.expires_at <= utc_now()))
            session.add(
                WebSession(
                    token_hash=token_hash(raw),
                    owner_id=owner_id,
                    csrf_token=csrf,
                    expires_at=expires,
                )
            )
        response.set_cookie(
            COOKIE_NAME,
            raw,
            max_age=lifetime,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return {"csrf_token": csrf, "expires_at": expires.isoformat()}

    @app.get("/api/auth/session")
    def auth_session(request: Request, response: Response) -> dict[str, Any]:
        if request.state.session_key:
            with session_factory() as session:
                row = session.get(WebSession, request.state.session_key)
                if row is not None:
                    return {
                        "authenticated": request.state.owner_username is not None,
                        "username": request.state.owner_username,
                        "csrf_token": row.csrf_token,
                        "expires_at": row.expires_at.isoformat(),
                        "live_trading_enabled": False,
                    }
        return {
            **issue_session(response, None),
            "authenticated": False,
            "username": None,
            "live_trading_enabled": False,
        }

    @app.post("/api/auth/login")
    def login(body: LoginInput, request: Request, response: Response) -> dict[str, Any]:
        now = utc_now()
        # Independent per-address and per-account counters resist username/password spraying.
        keys = [
            token_hash("ip:" + (request.client.host if request.client else "unknown")),
            token_hash("user:" + body.username.lower()),
        ]
        throttled = False
        with session_factory.begin() as session:
            session.execute(delete(WebLoginThrottle).where(WebLoginThrottle.reset_at <= now))
            for key in keys:
                statement = insert(WebLoginThrottle).values(
                    key=key, attempts=1, reset_at=now + timedelta(minutes=15)
                )
                attempts = session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["key"],
                        set_={"attempts": WebLoginThrottle.attempts + 1},
                    ).returning(WebLoginThrottle.attempts)
                ).scalar_one()
                throttled = throttled or attempts > 10
                if throttled:
                    break
        if throttled:
            raise HTTPException(
                429, "too many login attempts; retry later", headers={"Retry-After": "900"}
            )
        with session_factory.begin() as session:
            owner = session.scalar(select(WebOwner).where(WebOwner.username == body.username))
            valid = verify_password(body.password, owner.password_hash if owner else _DUMMY_HASH)
            if not valid or owner is None:
                raise HTTPException(401, "invalid username or password")
            owner_id, username = owner.id, owner.username
            session.execute(
                delete(WebSession).where(WebSession.token_hash == request.state.session_key)
            )
            session.add(
                AuditEvent(
                    actor=username,
                    action="web.login",
                    resource_type="web_owner",
                    resource_id=str(owner_id),
                    details={},
                )
            )
        return {
            **issue_session(response, owner_id),
            "authenticated": True,
            "username": username,
            "live_trading_enabled": False,
        }

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        with session_factory.begin() as session:
            session.execute(
                delete(WebSession).where(WebSession.token_hash == request.state.session_key)
            )
            session.add(
                AuditEvent(
                    actor=request.state.owner_username,
                    action="web.logout",
                    resource_type="web_session",
                    resource_id="current",
                    details={},
                )
            )
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict"
        )
        return {"authenticated": False}

    @app.get("/api/config/{kind}")
    def get_configuration(kind: str) -> dict[str, Any]:
        if kind not in {"risk", "strategy"}:
            raise HTTPException(404, "unknown configuration")
        with session_factory() as session:
            row = session.get(ApplicationConfiguration, "runtime")
            return {
                "version": row.version if row else 0,
                "configuration": _redact(row.configuration.get(kind)) if row else None,
                "defaults": StrategyConfiguration().model_dump(mode="json")
                if kind == "strategy"
                else None,
                "live_trading_enabled": False,
            }

    @app.put("/api/config/{kind}")
    def put_configuration(kind: str, body: ConfigurationInput, request: Request) -> dict[str, Any]:
        if kind not in {"risk", "strategy"}:
            raise HTTPException(404, "unknown configuration")
        try:
            with session_factory.begin() as session:
                if kind == "risk":
                    version = save_risk_configuration(
                        session,
                        RiskConfiguration.model_validate(body.configuration),
                        actor=request.state.owner_username,
                        expected_version=body.expected_version,
                    )
                else:
                    version = save_strategy_configuration(
                        session,
                        StrategyConfiguration.model_validate(body.configuration),
                        actor=request.state.owner_username,
                        expected_version=body.expected_version,
                    )
        except ValidationError as exc:
            raise HTTPException(422, _errors(exc)) from None
        except (ConfigurationConflict, IntegrityError):
            raise HTTPException(409, "configuration changed; reload before saving") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"version": version, "live_trading_enabled": False}

    @app.get("/api/audit")
    def audit(
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        with session_factory() as session:
            rows = session.scalars(
                select(AuditEvent)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "actor": row.actor,
                        "action": row.action,
                        "resource_type": row.resource_type,
                        "resource_id": row.resource_id,
                        "details": _redact(row.details),
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in rows
                ]
            }

    @app.get("/api/research")
    def research(
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        with session_factory() as session:
            rows = session.scalars(
                select(BacktestRun)
                .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "strategy_version": row.strategy_version,
                        "status": row.status,
                        "dataset_metadata": _redact(row.dataset_metadata),
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in rows
                ]
            }

    def research_record(run_id: str) -> dict[str, Any]:
        with session_factory() as session:
            row = session.get(BacktestRun, run_id)
            if row is None:
                raise HTTPException(404, "research run not found")
            return {
                "id": row.id,
                "strategy_version": row.strategy_version,
                "status": row.status,
                "configuration_snapshot": _redact(row.configuration_snapshot),
                "dataset_metadata": _redact(row.dataset_metadata),
                "metrics": _redact(row.metrics),
            }

    @app.get("/api/research/{run_id}/validation.json")
    def research_validation(run_id: str) -> dict[str, Any]:
        record = research_record(run_id)
        if record["dataset_metadata"].get("kind") != "walk_forward_search":
            raise HTTPException(404, "run has no walk-forward validation")
        return record

    @app.get("/api/research/{run_id}/trades.csv")
    def research_csv(run_id: str) -> Response:
        record = research_record(run_id)
        metrics = record["metrics"] or {}
        if record["dataset_metadata"].get("kind") == "walk_forward_search":
            metrics = metrics.get("selected_holdout") or metrics.get("baseline_holdout") or {}
        trades = metrics.get("trades", [])
        fields = list(trades[0]) if trades else ["id", "symbol", "direction"]
        return _csv(trades, fields)

    @app.get("/api/research/{run_id}")
    def research_detail(run_id: str) -> dict[str, Any]:
        return research_record(run_id)

    @app.get("/api/paper/capabilities")
    def paper_capabilities() -> dict[str, Any]:
        return {
            "available": True,
            "live_trading_enabled": False,
            "note": "Runs start paused. A separate worker processes closed bars; "
            "request handlers never contact the exchange.",
            "simulation_defaults": BacktestConfiguration.model_validate(
                {"starting_balance": "1000"}
            ).model_dump(mode="json"),
        }

    def paper_record(row: PaperRun) -> dict[str, Any]:
        return {
            "id": row.id,
            "version": row.version,
            "status": row.status,
            "symbol": row.symbol,
            "configuration": _redact(row.configuration),
            "state": _redact(row.state),
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "live_trading_enabled": False,
        }

    @app.get("/api/paper/runs")
    def paper_runs(
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        with session_factory() as session:
            rows = session.scalars(
                select(PaperRun)
                .order_by(PaperRun.created_at.desc(), PaperRun.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return {"items": [paper_record(row) for row in rows]}

    @app.post("/api/paper/runs", status_code=201)
    def paper_create(body: PaperCreateInput, request: Request) -> dict[str, Any]:
        try:
            with session_factory.begin() as session:
                row = create_paper_run(
                    session,
                    body.symbol,
                    body.simulation,
                    environment=body.environment,
                    start=body.start,
                    actor=request.state.owner_username,
                )
                result = paper_record(row)
        except (MissingRiskConfiguration, MissingStrategyConfiguration) as exc:
            raise HTTPException(409, str(exc)) from None
        except (ConfigurationConflict, IntegrityError):
            raise HTTPException(409, "paper state changed; reload before updating") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return result

    @app.get("/api/paper/runs/{run_id}")
    def paper_detail(run_id: str) -> dict[str, Any]:
        with session_factory() as session:
            row = session.get(PaperRun, run_id)
            if row is None:
                raise HTTPException(404, "paper run not found")
            return paper_record(row)

    @app.post("/api/paper/runs/{run_id}/control")
    def paper_control(run_id: str, body: PaperControlInput, request: Request) -> dict[str, Any]:
        try:
            with session_factory.begin() as session:
                if session.get(PaperRun, run_id) is None:
                    raise HTTPException(404, "paper run not found")
                row = control_paper_run(
                    session,
                    run_id,
                    body.action,
                    expected_version=body.expected_version,
                    actor=request.state.owner_username,
                )
                result = paper_record(row)
        except (ConfigurationConflict, IntegrityError):
            raise HTTPException(409, "paper state changed; reload before updating") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return result

    @app.get("/api/paper/runs/{run_id}/events")
    def paper_events(
        run_id: str,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with session_factory() as session:
            if session.get(PaperRun, run_id) is None:
                raise HTTPException(404, "paper run not found")
            rows = session.scalars(
                select(PaperEvent)
                .where(PaperEvent.run_id == run_id)
                .order_by(PaperEvent.bar_open.desc(), PaperEvent.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "bar_open": row.bar_open.isoformat(),
                        "details": _redact(row.details),
                    }
                    for row in rows
                ]
            }

    if settings.frontend_dist is not None:
        root = Path(settings.frontend_dist).resolve()

        @app.get("/{asset_path:path}", include_in_schema=False)
        def frontend(asset_path: str) -> FileResponse:
            if asset_path.startswith(("api/", "docs", "redoc", "openapi.json")):
                raise HTTPException(404, "not found")
            candidate = (root / asset_path).resolve()
            if not candidate.is_relative_to(root):
                raise HTTPException(404, "not found")
            if candidate.is_file():
                return FileResponse(candidate)
            if asset_path and "." in asset_path.rsplit("/", 1)[-1]:
                raise HTTPException(404, "asset not found")
            index = root / "index.html"
            if not index.is_file():
                raise HTTPException(503, "dashboard build not available")
            return FileResponse(index)
