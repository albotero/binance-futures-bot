import json
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from futures_bot.exchange import BinanceAPIError, ExchangeInformation
from futures_bot.exchange.leverage import LeverageProvider
from futures_bot.persistence.database import make_session_factory
from futures_bot.persistence.models import (
    ApplicationConfiguration,
    AuditEvent,
    BacktestRun,
    Base,
    utc_now,
)
from futures_bot.persistence.repositories import save_application_configuration
from futures_bot.web import WebAuthSettings, bootstrap_owner, configure_web
from futures_bot.web.auth import hash_password, token_hash, verify_password
from futures_bot.web.models import WebOwner, WebSession

ORIGIN = "http://localhost"
PASSWORD = "correct horse battery staple 123!"


@pytest.fixture
def web(request, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = make_session_factory(engine)
    Base.metadata.create_all(engine)
    bootstrap_owner(factory, "owner", PASSWORD)
    app = FastAPI()
    provider = None
    if getattr(request, "param", False):
        keys = tmp_path / "keys.json"
        keys.write_text(json.dumps({"api_key": "fake-key", "api_secret": "fake-secret"}))
        keys.chmod(0o600)

        def handler(req):
            if req.url.path.endswith("/time"):
                return httpx2.Response(200, json={"serverTime": 1700000000123})
            return httpx2.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDC",
                        "brackets": [
                            {"initialLeverage": 125, "notionalFloor": "0", "notionalCap": "1000"},
                            {
                                "initialLeverage": 50,
                                "notionalFloor": "1000",
                                "notionalCap": "10000",
                            },
                        ],
                    }
                ],
            )

        provider = LeverageProvider(keys, transport=httpx2.MockTransport(handler))
        if request.param == "missing":
            keys.unlink()
    configure_web(
        app,
        factory,
        WebAuthSettings(allowed_origins=(ORIGIN,), cookie_secure=False, local_mode=True),
        leverage_provider=provider,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, factory, app
    engine.dispose()


def csrf(client):
    return client.get("/api/auth/session").json()["csrf_token"]


def login(client):
    previous = client.cookies.get("futures_session")
    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf(client)},
        json={"username": "owner", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["live_trading_enabled"] is False
    if previous:
        assert previous != client.cookies.get("futures_session")
    return response.json()["csrf_token"]


def risk_config():
    return {
        "quote_asset": "USDC",
        "policy": {
            "basis": "available_balance",
            "risk_fraction": "0.005",
            "margin_reserve_fraction": "0.2",
            "maximum_position_notional": "1000",
            "maximum_total_exposure": "3000",
            "maximum_aggregate_risk": "30",
            "maximum_daily_loss": "50",
            "maximum_drawdown_fraction": "0.1",
            "maximum_stop_distance_fraction": "0.05",
            "minimum_reward_to_risk": "2",
            "maximum_open_positions": 3,
            "maximum_leverage": 5,
        },
        "symbols": [{"symbol": "BTCUSDC", "leverage": 2, "enabled": True}],
    }


def test_uninitialized_strategy_provides_all_editable_defaults(web):
    client, factory, _ = web
    login(client)
    response = client.get("/api/config/strategy")
    assert response.status_code == 200
    data = response.json()
    assert data["configuration"] is None
    assert data["defaults"] == {
        "trend_fast_period": 50,
        "trend_slow_period": 200,
        "pullback_period": 20,
        "atr_period": 14,
        "swing_period": 5,
        "atr_multiplier": "1.5",
        "reward_multiple": "2",
        "entry_expiry_bars": 3,
        "time_stop_bars": 8,
        "protection_mode": "support_resistance",
        "level_lookback_bars": 96,
        "pivot_confirmation_bars": 2,
    }
    with factory() as session:
        assert session.get(ApplicationConfiguration, "runtime") is None


@pytest.mark.parametrize("web", [True], indirect=True)
def test_account_brackets_are_authenticated_and_enforced_on_save(web):
    client, factory, _ = web
    assert client.get("/api/market/leverage").status_code == 401
    token = login(client)
    response = client.get("/api/market/leverage")
    assert response.status_code == 200
    assert response.json()["exchange_leverage_verified"] is True
    assert response.json()["items"][0]["maximum_leverage"] == 125
    assert "fake-secret" not in response.text and "fake-key" not in response.text
    config = risk_config()
    config["symbols"][0]["leverage"] = 125
    config["symbols"][0]["leverage_schedule"] = {
        "symbol": "BTCUSDC",
        "fetched_at": "2026-10-16T12:00:00Z",
        "brackets": [{"initial_leverage": 200, "notional_floor": "0", "notional_cap": "999999999"}],
    }
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token}
    response = client.put(
        "/api/config/risk",
        headers=headers,
        json={"expected_version": 0, "configuration": config},
    )
    assert response.status_code == 200
    saved = client.get("/api/config/risk").json()["configuration"]
    assert saved["symbols"][0]["leverage"] == 125
    assert saved["symbols"][0]["leverage_schedule"]["brackets"][0]["notional_cap"] == "1000"
    config["symbols"][0]["leverage"] = 126
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 1, "configuration": config},
        ).status_code
        == 422
    )
    config["symbols"][0] = {"symbol": "ETHUSDC", "enabled": True, "leverage": 1}
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 1, "configuration": config},
        ).status_code
        == 422
    )
    with factory() as session:
        assert session.get(ApplicationConfiguration, "runtime").version == 1
    strategy = client.get("/api/config/strategy").json()["defaults"]
    assert (
        client.put(
            "/api/config/strategy",
            headers=headers,
            json={"expected_version": 1, "configuration": strategy},
        ).status_code
        == 200
    )
    paper = client.post(
        "/api/paper/runs",
        headers=headers,
        json={
            "symbol": "BTCUSDC",
            "start": "2026-10-16T12:00:00Z",
            "environment": "production",
            "simulation": {"starting_balance": "10000"},
        },
    )
    assert paper.status_code == 201, paper.text
    assert paper.json()["status"] == "paused"
    snapshot = paper.json()["configuration"]["risk"]["symbols"][0]
    assert snapshot["leverage"] == 125
    assert snapshot["leverage_schedule"]["brackets"][0]["notional_cap"] == "1000"


@pytest.mark.parametrize("web", ["missing"], indirect=True)
def test_missing_credentials_block_verified_risk_saves_without_mutating_settings(web):
    client, factory, _ = web
    token = login(client)
    assert client.get("/api/market/leverage").status_code == 503
    response = client.put(
        "/api/config/risk",
        headers={"Origin": ORIGIN, "X-CSRF-Token": token},
        json={"expected_version": 0, "configuration": risk_config()},
    )
    assert response.status_code == 503
    assert "fake-secret" not in response.text
    with factory() as session:
        assert session.get(ApplicationConfiguration, "runtime") is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/config/risk",
        "/api/config/strategy",
        "/api/audit",
        "/api/research",
        "/api/research/nope/trades.csv",
        "/api/research/nope/validation.json",
        "/api/paper/capabilities",
        "/api/market/contracts",
    ],
)
def test_auth_gates(web, path):
    client, _, _ = web
    assert client.get(path).status_code == 401
    csrf(client)
    assert client.get(path).status_code == 401


def test_login_rotation_logout_and_csrf(web):
    client, factory, _ = web
    preauth_csrf = csrf(client)
    preauth_cookie = client.cookies.get("futures_session")
    assert client.post("/api/auth/login", json={}).status_code == 403
    assert client.post("/api/auth/login", headers={"Origin": ORIGIN}, json={}).status_code == 403
    token = login(client)
    assert token != preauth_csrf
    assert client.cookies.get("futures_session") != preauth_cookie
    with factory() as session:
        assert session.get(WebSession, token_hash(preauth_cookie)) is None
        assert session.get(WebSession, token_hash(client.cookies["futures_session"])) is not None
    assert client.get("/api/audit").status_code == 200
    assert (
        client.post(
            "/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": preauth_csrf}
        ).status_code
        == 403
    )
    response = client.post(
        "/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": token}, json={}
    )
    assert response.status_code == 200
    assert client.get("/api/audit").status_code == 401
    with factory() as session:
        assert session.scalar(select(WebSession).where(WebSession.owner_id == 1)) is None


def test_cookie_security_and_expiry(web):
    client, factory, _ = web
    response = client.get("/api/auth/session")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    token = login(client)
    with factory.begin() as session:
        row = session.get(WebSession, token_hash(client.cookies["futures_session"]))
        row.expires_at = utc_now() - timedelta(seconds=1)
    assert client.get("/api/audit").status_code == 401
    assert (
        client.put(
            "/api/config/risk",
            headers={"Origin": ORIGIN, "X-CSRF-Token": token},
            json={"expected_version": 0, "configuration": risk_config()},
        ).status_code
        == 401
    )
    assert client.get("/api/auth/session").json()["authenticated"] is False


def test_secure_default_and_origin_rules():
    with pytest.raises(ValueError):
        WebAuthSettings(allowed_origins=("http://example.com",), local_mode=True)
    with pytest.raises(ValueError):
        WebAuthSettings(allowed_origins=("https://example.com",), cookie_secure=False)
    with pytest.raises(ValueError):
        WebAuthSettings(allowed_origins=("https://example.com/",))
    assert WebAuthSettings(allowed_origins=("https://example.com",)).cookie_secure is True


def test_host_origin_enforcement_and_docs(web):
    client, _, _ = web
    assert client.get("/api/auth/session", headers={"Host": "evil.example"}).status_code == 400
    assert (
        client.get("/api/auth/session", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        client.get("/api/auth/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    )
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    token = login(client)
    assert (
        client.put(
            "/api/config/risk",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
            json={},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_cross_site_top_level_entry_can_load_public_dashboard(web, method):
    client, _, app = web

    @app.api_route("/", methods=["GET", "HEAD"])
    def dashboard():
        return {"public_shell": True}

    headers = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    response = client.request(method, "/", headers=headers)
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert client.get("/api/audit").status_code == 401
    assert client.get("/api/auth/session", headers=headers).status_code == 403
    assert client.post("/", headers=headers).status_code == 403
    assert client.get("/", headers={**headers, "Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    token = login(client)
    assert (
        client.post(
            "/api/auth/logout",
            headers={**headers, "Origin": ORIGIN, "X-CSRF-Token": token},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "mode,destination",
    [("navigate", "iframe"), ("cors", "empty"), ("no-cors", "image"), ("", "")],
)
def test_cross_site_entry_exception_excludes_embeds_and_fetches(web, mode, destination):
    client, _, _ = web
    assert (
        client.get(
            "/",
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": mode,
                "Sec-Fetch-Dest": destination,
            },
        ).status_code
        == 403
    )


def test_login_throttle_and_generic_failure(web):
    client, _, _ = web
    token = csrf(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token}
    for index in range(10):
        response = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "owner" if index % 2 else "unknown", "password": "wrong"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid username or password"
    response = client.post(
        "/api/auth/login", headers=headers, json={"username": "owner", "password": PASSWORD}
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "900"


def test_stale_configuration_and_preserve_shared_fields(web):
    client, factory, _ = web
    with factory.begin() as session:
        save_application_configuration(session, {"other": {"preserve": True}}, "test", 0)
    token = login(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token}
    response = client.put(
        "/api/config/risk",
        headers=headers,
        json={"expected_version": 1, "configuration": risk_config()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    strategy = client.get("/api/config/strategy").json()["defaults"]
    response = client.put(
        "/api/config/strategy",
        headers=headers,
        json={"expected_version": 1, "configuration": strategy},
    )
    assert response.status_code == 409
    response = client.put(
        "/api/config/strategy",
        headers=headers,
        json={"expected_version": 2, "configuration": strategy},
    )
    assert response.status_code == 200
    with factory() as session:
        row = session.get(ApplicationConfiguration, "runtime")
        assert row.version == 3
        assert row.configuration["other"] == {"preserve": True}
        assert row.configuration["risk"]["quote_asset"] == "USDC"
        assert row.configuration["strategy"] == strategy
    assert client.get("/api/config/risk").json()["version"] == 3


def test_config_validation_no_credentials_and_no_live_actions(web):
    client, factory, _ = web
    token = login(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token}
    config = risk_config()
    config["symbols"][0]["leverage"] = 0
    response = client.put(
        "/api/config/risk",
        headers=headers,
        json={"expected_version": 0, "configuration": config},
    )
    assert response.status_code == 422
    assert "leverage" in response.text
    assert "input" not in response.text
    config = risk_config()
    config["quote_asset"] = "USDT"
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 0, "configuration": config},
        ).status_code
        == 422
    )
    assert client.post("/api/trade", headers=headers, json={}).status_code == 404
    assert client.get("/api/paper/capabilities").json()["available"] is True
    with factory() as session:
        assert session.get(ApplicationConfiguration, "runtime") is None


def test_contract_directory_filters_and_caches_public_metadata(web, monkeypatch):
    client, _, _ = web
    requests = []
    clock = [100.0]
    monkeypatch.setattr("futures_bot.web.api.monotonic", lambda: clock[0])
    entries = [
        ("ETHUSDC", "ETH", "USDC", "USDC", "TRADING", "PERPETUAL"),
        ("BTCUSDT", "BTC", "USDT", "USDT", "TRADING", "PERPETUAL"),
        ("BTCUSDC", "BTC", "USDC", "USDC", "TRADING", "PERPETUAL"),
        ("MISMATCHUSDC", "MISMATCH", "USDC", "USDT", "TRADING", "PERPETUAL"),
        ("HALTEDUSDC", "HALTED", "USDC", "USDC", "BREAK", "PERPETUAL"),
        ("DATEDUSDC", "DATED", "USDC", "USDC", "TRADING", "CURRENT_QUARTER"),
    ]
    information = ExchangeInformation.model_validate(
        {
            "symbols": [
                {
                    "symbol": symbol,
                    "baseAsset": base,
                    "quoteAsset": quote,
                    "marginAsset": margin,
                    "status": status,
                    "contractType": contract,
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.01",
                            "maxPrice": "1000000",
                            "tickSize": "0.01",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "1000",
                            "stepSize": "0.001",
                        },
                    ],
                }
                for symbol, base, quote, margin, status, contract in entries
            ]
        }
    )

    class PublicClient:
        def __init__(self, environment):
            requests.append(environment)
            self.environment = environment

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def exchange_information(self):
            return (
                information if self.environment == "production" else ExchangeInformation(symbols=())
            )

    monkeypatch.setattr("futures_bot.web.api.BinanceUSDmClient", PublicClient)
    assert client.get("/api/market/contracts").status_code == 401
    assert requests == []
    login(client)
    response = client.get("/api/market/contracts")
    assert response.status_code == 200
    result = response.json()
    assert result["quote_asset"] == "USDC" and result["environment"] == "production"
    assert result["unverified_simulation_max_leverage"] == 50
    assert result["exchange_leverage_verified"] is False
    assert [item["symbol"] for item in result["items"]] == ["BTCUSDC", "ETHUSDC"]
    assert result["items"][0]["base_asset"] == "BTC"
    usdt = client.get("/api/market/contracts?quote_asset=USDT").json()
    assert [item["symbol"] for item in usdt["items"]] == ["BTCUSDT"]
    assert requests == ["production"]
    assert client.get("/api/market/contracts?environment=testnet").json()["items"] == []
    assert requests == ["production", "testnet"]
    assert len(client.get("/api/market/contracts").json()["items"]) == 2
    clock[0] = 160.0
    client.get("/api/market/contracts")
    assert requests == ["production", "testnet", "production"]
    assert client.get("/api/market/contracts?quote_asset=USD").status_code == 422
    assert client.get("/api/market/contracts?environment=live").status_code == 422


def test_contract_directory_reports_outage_without_stale_success(web, monkeypatch):
    client, _, _ = web

    class UnavailableClient:
        def __init__(self, environment):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def exchange_information(self):
            raise BinanceAPIError("sensitive exchange response")

    monkeypatch.setattr("futures_bot.web.api.BinanceUSDmClient", UnavailableClient)
    login(client)
    response = client.get("/api/market/contracts")
    assert response.status_code == 502
    assert "retry" in response.json()["detail"]
    assert "sensitive" not in response.text


def test_web_accepts_50x_without_separate_portfolio_limit(web):
    client, _, _ = web
    token = login(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token}
    config = risk_config()
    config["policy"]["maximum_leverage"] = 50
    config["symbols"][0]["leverage"] = 50
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 0, "configuration": config},
        ).status_code
        == 200
    )
    assert client.get("/api/config/risk").json()["configuration"]["symbols"][0]["leverage"] == 50
    config["policy"]["maximum_leverage"] = 49
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 1, "configuration": config},
        ).status_code
        == 200
    )
    config["symbols"][0]["leverage"] = 0
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 2, "configuration": config},
        ).status_code
        == 422
    )
    response = client.post(
        "/api/auth/login",
        headers=headers,
        json={"username": "owner", "password": {"secret": PASSWORD}},
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert "password_hash" not in client.get("/api/audit").text


def test_password_hashing_and_bootstrap_guard(web):
    _, factory, _ = web
    encoded = hash_password(PASSWORD)
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("wrong", encoded)
    assert encoded != hash_password(PASSWORD)
    assert PASSWORD not in encoded
    with pytest.raises(ValueError):
        bootstrap_owner(factory, "another", PASSWORD)
    with pytest.raises(ValueError):
        hash_password("short")
    with factory() as session:
        assert session.get(WebOwner, 1).username == "owner"


def test_research_exports_and_redaction(web):
    client, factory, _ = web
    with factory.begin() as session:
        session.add(
            AuditEvent(
                actor="tester",
                action="research",
                resource_type="backtest",
                details={"api_key": "sensitive-api-key", "safe": "shown"},
            )
        )
        session.add(
            BacktestRun(
                id="validation-run",
                strategy_version="v1",
                configuration_snapshot={"secret": "hidden"},
                dataset_metadata={"kind": "walk_forward_search"},
                status="completed",
                metrics={"baseline_holdout": {"trades": [{"symbol": "=FORMULA()", "pnl": "-1"}]}},
            )
        )
    login(client)
    audit = client.get("/api/audit")
    assert "sensitive-api-key" not in audit.text
    assert "shown" in audit.text
    assert client.get("/api/research").json()["items"][0]["id"] == "validation-run"
    response = client.get("/api/research/validation-run/validation.json")
    assert response.status_code == 200
    assert "hidden" not in response.text
    response = client.get("/api/research/validation-run/trades.csv")
    assert response.status_code == 200
    assert "'=FORMULA()" in response.text
    assert "'-1" in response.text
    assert client.get("/api/research/missing").status_code == 404


def test_static_dist_no_traversal_or_public_docs(web):
    client, factory, _ = web
    root = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if not root.is_dir():
        pytest.skip("compile frontend first to verify static serving")
    app = FastAPI()
    configure_web(
        app,
        factory,
        WebAuthSettings(
            allowed_origins=(ORIGIN,), cookie_secure=False, local_mode=True, frontend_dist=root
        ),
    )
    with TestClient(app, base_url=ORIGIN) as static:
        assert static.get("/").status_code == 200
        assert "Futures Operator" in static.get("/").text
        for path, content_type in (
            ("/icon.svg", "image/svg+xml"),
            ("/favicon.ico", "image/"),
            ("/apple-touch-icon.png", "image/png"),
        ):
            assert f'href="{path}"' in static.get("/").text
            icon = static.get(path)
            assert icon.status_code == 200
            assert icon.headers["content-type"].startswith(content_type)
            assert icon.content
        assert 'viewBox="0 0 64 64"' in static.get("/icon.svg").text
        assert static.get("/docs").status_code == 404
        assert static.get("/missing.js").status_code == 404
        assert static.get("/..%2F..%2Fpyproject.toml").status_code == 404
        assert static.get("/api/audit").status_code == 401
        response = static.get("/")
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"


def test_paper_configuration_controls_and_stale_writes(web):
    client, _, _ = web
    token = login(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token}
    body = {
        "symbol": "BTCUSDC",
        "simulation": {"starting_balance": "1000"},
        "environment": "testnet",
        "start": "2026-10-16T00:00:00Z",
    }
    assert client.post("/api/paper/runs", headers=headers, json=body).status_code == 409
    assert (
        client.put(
            "/api/config/risk",
            headers=headers,
            json={"expected_version": 0, "configuration": risk_config()},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/config/strategy",
            headers=headers,
            json={"expected_version": 1, "configuration": {}},
        ).status_code
        == 200
    )
    assert client.post("/api/paper/runs", headers={"Origin": ORIGIN}, json=body).status_code == 403
    response = client.post("/api/paper/runs", headers=headers, json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "paused"
    assert run["live_trading_enabled"] is False
    assert run["configuration"]["risk"]["quote_asset"] == "USDC"
    assert run["state"]["cash"] == "1000"
    assert client.get("/api/paper/runs").json()["items"][0]["id"] == run["id"]
    assert client.get(f"/api/paper/runs/{run['id']}/events").json() == {"items": []}
    response = client.post(
        f"/api/paper/runs/{run['id']}/control",
        headers=headers,
        json={"expected_version": run["version"], "action": "resume"},
    )
    assert response.status_code == 200, response.text
    running = response.json()
    assert running["status"] == "running"
    assert (
        client.post(
            f"/api/paper/runs/{run['id']}/control",
            headers=headers,
            json={"expected_version": run["version"], "action": "pause"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/paper/runs/{run['id']}/control",
            headers=headers,
            json={"expected_version": running["version"], "action": "stop"},
        ).json()["status"]
        == "stopped"
    )
    assert client.get("/api/paper/runs/missing").status_code == 404


def test_parent_routes_inherit_auth_and_csrf(web):
    client, _, app = web

    @app.post("/api/research/run")
    def research_run():
        return {"safe": True}

    assert client.post("/api/research/run", headers={"Origin": ORIGIN}).status_code == 401
    token = login(client)
    assert client.post("/api/research/run", headers={"Origin": ORIGIN}).status_code == 403
    assert (
        client.post(
            "/api/research/run", headers={"Origin": ORIGIN, "X-CSRF-Token": token}
        ).status_code
        == 200
    )
