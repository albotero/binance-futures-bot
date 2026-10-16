import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import ValidationError

from futures_bot.exchange.leverage import (
    LeverageBracket,
    LeverageLookupError,
    LeverageProvider,
    LeverageSchedule,
)


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"api_key": "test-read-only-key", "api_secret": "test-signing-secret"})
    )
    path.chmod(0o600)
    return path


def payload():
    return [
        {
            "symbol": "BTCUSDC",
            "brackets": [
                {"initialLeverage": 125, "notionalFloor": "0", "notionalCap": "1000"},
                {"initialLeverage": 50, "notionalFloor": "1000", "notionalCap": "10000"},
                {"initialLeverage": 10, "notionalFloor": "10000", "notionalCap": "100000"},
            ],
        }
    ]


def test_read_only_signature_cache_and_exact_account_brackets(credentials):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "fapi.binance.com"
        if request.url.path == "/fapi/v1/time":
            return httpx2.Response(200, json={"serverTime": 1700000000123})
        assert request.url.path == "/fapi/v1/leverageBracket"
        assert request.headers["X-MBX-APIKEY"] == "test-read-only-key"
        query = request.url.query.decode()
        signed, signature = query.rsplit("&signature=", 1)
        assert (
            signature
            == hmac.new(b"test-signing-secret", signed.encode(), hashlib.sha256).hexdigest()
        )
        assert parse_qs(signed) == {"timestamp": ["1700000000123"], "recvWindow": ["5000"]}
        return httpx2.Response(200, json=payload())

    async def run():
        provider = LeverageProvider(credentials, transport=httpx2.MockTransport(handler))
        first = await provider.schedules()
        assert await provider.schedules() is first
        schedule = first[0]
        assert schedule.maximum_leverage == 125
        assert schedule.notional_limit(125) == Decimal("1000")
        assert schedule.notional_limit(50) == Decimal("10000")
        assert schedule.notional_limit(1) == Decimal("100000")
        with pytest.raises(ValueError, match="verified Binance maximum"):
            schedule.notional_limit(126)
        serialized = schedule.model_dump_json()
        assert "test-signing" not in serialized and "test-read-only" not in serialized

    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize("status", [301, 401, 403, 418, 429, 500])
def test_bracket_errors_never_follow_redirects_or_expose_credentials(credentials, status):
    def handler(request):
        if request.url.path.endswith("/time"):
            return httpx2.Response(200, json={"serverTime": 1700000000123})
        return httpx2.Response(
            status,
            json={"msg": "sensitive exchange error"},
            headers={"Location": "https://untrusted.example/"},
        )

    provider = LeverageProvider(credentials, transport=httpx2.MockTransport(handler))
    with pytest.raises(LeverageLookupError, match=f"HTTP {status}") as error:
        asyncio.run(provider.schedules())
    assert "sensitive" not in str(error.value)
    assert "test-signing-secret" not in str(error.value)
    assert "signature" not in str(error.value)


def test_expired_brackets_are_not_returned_as_success_on_outage(credentials, monkeypatch):
    now = [0]
    monkeypatch.setattr("futures_bot.exchange.leverage.monotonic", lambda: now[0])
    failing = [False]

    def handler(request):
        if request.url.path.endswith("/time"):
            return httpx2.Response(200, json={"serverTime": 1700000000123})
        return httpx2.Response(503 if failing[0] else 200, json=payload())

    async def run():
        provider = LeverageProvider(credentials, transport=httpx2.MockTransport(handler))
        await provider.schedules()
        now[0] = 61
        failing[0] = True
        with pytest.raises(LeverageLookupError):
            await provider.schedules()

    asyncio.run(run())


@pytest.mark.parametrize("mode", [0o644, 0o660])
def test_credentials_must_be_private(credentials, mode):
    credentials.chmod(mode)
    with pytest.raises(LeverageLookupError, match="private"):
        asyncio.run(LeverageProvider(credentials).schedules())


def test_credentials_symlink_and_missing_file_are_refused(credentials):
    link = credentials.with_name("link.json")
    link.symlink_to(credentials)
    for path in (link, credentials.with_name("missing.json")):
        with pytest.raises(LeverageLookupError, match="missing or invalid"):
            asyncio.run(LeverageProvider(path).schedules())


@pytest.mark.parametrize("change", ["gap", "increasing", "nan", "negative", "empty"])
def test_invalid_brackets_are_rejected(credentials, change):
    data = payload()
    brackets = data[0]["brackets"]
    if change == "gap":
        brackets[1]["notionalFloor"] = "1001"
    elif change == "increasing":
        brackets[1]["initialLeverage"] = 150
    elif change == "nan":
        brackets[0]["notionalCap"] = "NaN"
    elif change == "negative":
        brackets[0]["notionalFloor"] = "-1"
    else:
        data[0]["brackets"] = []

    def handler(request):
        return httpx2.Response(
            200, json={"serverTime": 1700000000123} if request.url.path.endswith("/time") else data
        )

    with pytest.raises(LeverageLookupError, match="unavailable or invalid"):
        asyncio.run(
            LeverageProvider(credentials, transport=httpx2.MockTransport(handler)).schedules()
        )


def test_actual_exchange_maximum_not_a_hardcoded_125_ceiling():
    schedule = LeverageSchedule(
        symbol="ETHUSDT",
        fetched_at=datetime.now(UTC),
        brackets=(LeverageBracket(initial_leverage=150, notional_floor="0", notional_cap="10000"),),
    )
    assert schedule.maximum_leverage == 150
    assert schedule.notional_limit(150) == Decimal("10000")
    with pytest.raises(ValidationError):
        LeverageBracket(initial_leverage=True, notional_floor="0", notional_cap="100")
