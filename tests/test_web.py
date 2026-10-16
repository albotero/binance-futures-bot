from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

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
def web():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = make_session_factory(engine)
    Base.metadata.create_all(engine)
    bootstrap_owner(factory, "owner", PASSWORD)
    app = FastAPI()
    configure_web(
        app,
        factory,
        WebAuthSettings(allowed_origins=(ORIGIN,), cookie_secure=False, local_mode=True),
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
    }
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
    config["symbols"][0]["leverage"] = 21
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
