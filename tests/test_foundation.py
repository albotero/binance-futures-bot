import pytest
from fastapi.testclient import TestClient

from futures_bot.app import create_app
from futures_bot.config import Settings


def test_settings_default_to_paper_testnet_and_private_database() -> None:
    settings = Settings(_env_file=None)

    assert settings.mode == "paper"
    assert settings.exchange_environment == "testnet"
    assert settings.database_url == "sqlite:///./data/futures_platform.sqlite3"
    assert settings.reporting_timezone == "America/Bogota"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "live"),
        ("exchange_environment", "mainnet"),
    ],
)
def test_settings_reject_unsupported_trading_state(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_health_is_liveness_only_and_live_trading_is_disabled() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "alive",
        "mode": "paper",
        "exchange_environment": "testnet",
        "database": "not_initialized",
        "live_trading_enabled": False,
    }


def test_api_documentation_is_not_exposed_before_authentication() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
