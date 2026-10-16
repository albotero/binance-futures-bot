import pytest
from alembic.config import Config
from fastapi.testclient import TestClient

from alembic import command
from futures_bot.app import create_app
from futures_bot.config import Settings


def test_composed_dashboard_checks_schema_and_authenticates_parent_research_route(
    tmp_path, monkeypatch
):
    url = f"sqlite:///{tmp_path / 'dashboard.sqlite3'}"
    monkeypatch.setenv("BOT_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html><body>operator</body></html>", encoding="utf-8")
    settings = Settings(
        dashboard_enabled=True,
        database_url=url,
        public_origin="http://127.0.0.1",
        secure_cookies=False,
        frontend_directory=str(frontend),
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get("/health").json()["database"] == "ready"
        assert client.get("/ready").json()["live_trading_enabled"] is False
        assert client.post("/api/research/run", json={}).status_code == 403
        assert (
            client.post(
                "/api/research/run", json={}, headers={"Origin": "http://127.0.0.1"}
            ).status_code
            == 401
        )
        assert client.get("/api/config").status_code == 401
        assert client.get("/docs").status_code == 404


def test_dashboard_refuses_missing_migrations_instead_of_creating_schema(tmp_path):
    settings = Settings(
        dashboard_enabled=True,
        database_url=f"sqlite:///{tmp_path / 'missing.sqlite3'}",
        public_origin="http://127.0.0.1",
        secure_cookies=False,
    )
    from sqlalchemy.exc import OperationalError

    with pytest.raises(OperationalError, match="alembic_version"):
        with TestClient(create_app(settings), base_url="http://127.0.0.1"):
            pass


def test_insecure_remote_dashboard_cookies_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        create_app(
            Settings(
                dashboard_enabled=True,
                database_url=f"sqlite:///{tmp_path / 'blocked.sqlite3'}",
                public_origin="http://example.com",
                secure_cookies=False,
            )
        )
