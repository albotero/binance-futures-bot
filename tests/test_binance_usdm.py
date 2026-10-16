import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Literal

import httpx2
import pytest

from futures_bot.exchange import (
    BinanceAPIError,
    BinancePayloadError,
    BinanceUSDmClient,
    ClockSkewError,
    ExchangeInformation,
    ExchangeSymbol,
    MarkPrice,
    SymbolPriceTicker,
)


def run_client[T](
    client: BinanceUSDmClient,
    operation: Callable[[BinanceUSDmClient], Awaitable[T]],
) -> T:
    async def run() -> T:
        async with client:
            return await operation(client)

    return asyncio.run(run())


def test_public_server_time_and_environment_hosts() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"serverTime": 1_700_000_000_123})

    environments: tuple[tuple[Literal["testnet", "production"], str], ...] = (
        ("testnet", "testnet.binancefuture.com"),
        ("production", "fapi.binance.com"),
    )
    for environment, expected_host in environments:
        result = run_client(
            BinanceUSDmClient(
                environment,
                transport=httpx2.MockTransport(handler),
            ),
            lambda client: client.server_time(),
        )

        assert result.isoformat() == "2023-11-14T22:13:20.123000+00:00"
        assert requests[-1].url.host == expected_host
        assert requests[-1].url.path == "/fapi/v1/time"


@pytest.mark.parametrize(
    "create_client",
    [
        lambda: BinanceUSDmClient(environment="unknown"),  # type: ignore[arg-type]
        lambda: BinanceUSDmClient(timeout_seconds=float("nan")),
        lambda: BinanceUSDmClient(timeout_seconds=121),
        lambda: BinanceUSDmClient(max_retries=6),
        lambda: BinanceUSDmClient(backoff_seconds=float("inf")),
    ],
)
def test_client_rejects_unbounded_configuration(
    create_client: Callable[[], BinanceUSDmClient],
) -> None:
    with pytest.raises(ValueError):
        create_client()


@pytest.mark.parametrize("symbol", ["BTCUSDT", "\u5e01\u5b89\u4eba\u751fUSDT"])
def test_exchange_information_validates_public_symbol_metadata(symbol: str) -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": symbol,
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
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
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0",
                                "maxQty": "1000",
                                "stepSize": "0",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            },
        )

    information = run_client(
        BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
        lambda client: client.exchange_information(),
    )

    assert information.symbols[0].symbol == symbol
    assert information.symbols[0].contract_type == "PERPETUAL"
    assert information.symbols[0].quantity_precision == 3
    rules = information.symbols[0].rules()
    assert rules.price_tick == Decimal("0.01")
    assert rules.quantity_step == Decimal("0.001")
    assert rules.market_quantity_step == Decimal("0")
    assert rules.min_notional == Decimal("5")
    assert rules.quote_asset == "USDT"
    assert rules.margin_asset == "USDT"


def test_symbol_discovery_defaults_to_usdc_using_exchange_metadata() -> None:
    def metadata(symbol: str, quote: str, margin: str, status: str = "TRADING") -> ExchangeSymbol:
        return ExchangeSymbol.model_validate(
            {
                "symbol": symbol,
                "status": status,
                "contractType": "PERPETUAL",
                "baseAsset": "BTC",
                "quoteAsset": quote,
                "marginAsset": margin,
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.1",
                        "maxPrice": "1000000",
                        "tickSize": "0.1",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                ],
            }
        )

    information = ExchangeInformation(
        symbols=(
            metadata("BTCUSDC", "USDC", "USDC"),
            metadata("BTCUSDT", "USDT", "USDT"),
            metadata("ETHUSDC", "USDC", "USDT"),
            metadata("SOLUSDC", "USDC", "USDC", "SETTLING"),
            metadata("\u5e01\u5b89\u4eba\u751fUSDT", "USDT", "USDT"),
        )
    )
    assert [symbol.symbol for symbol in information.tradable_symbols()] == ["BTCUSDC"]
    assert [symbol.symbol for symbol in information.tradable_symbols("USDT")] == [
        "BTCUSDT",
        "\u5e01\u5b89\u4eba\u751fUSDT",
    ]


def test_unicode_symbol_queries_are_encoded_without_changing_identity() -> None:
    symbol = "\u5e01\u5b89\u4eba\u751fUSDT"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["symbol"] == symbol
        return httpx2.Response(200, json={"symbol": symbol, "price": "1.23"})

    tickers = run_client(
        BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
        lambda client: client.symbol_price_tickers(symbol),
    )
    assert tickers[0].symbol == symbol


@pytest.mark.parametrize("symbol", ["BTC/USDT", "BTC&USDT", "BTC\nUSDT", "btcusdt", "=BTCUSDT"])
def test_symbol_validation_still_rejects_unsafe_or_lowercase_names(symbol: str) -> None:
    from futures_bot.exchange.binance_usdm import validate_symbol_name

    with pytest.raises(ValueError):
        validate_symbol_name(symbol)


def test_clock_skew_rejects_a_local_clock_outside_tolerance() -> None:
    from datetime import UTC, datetime

    from futures_bot.exchange import BinanceUSDmClient

    fixed_local_time = datetime(2026, 10, 15, tzinfo=UTC)

    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"serverTime": int(fixed_local_time.timestamp() * 1000) + 5_000},
        )

    async def check() -> None:
        async with BinanceUSDmClient(
            transport=httpx2.MockTransport(handler),
            backoff_seconds=0,
            wall_clock=lambda: fixed_local_time,
        ) as client:
            with pytest.raises(ClockSkewError, match="differs from Binance"):
                await client.clock_skew(max_skew_seconds=1)

    asyncio.run(check())


def test_klines_normalize_decimal_values_and_query_parameters() -> None:
    requests: list[httpx2.Request] = []
    row = [
        1_700_000_000_000,
        "100.10",
        "105.00",
        "99.00",
        "103.50",
        "12.25",
        1_700_000_059_999,
        "1250.00",
        42,
        "5.50",
        "560.00",
        "0",
    ]

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=[row])

    candles = run_client(
        BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
        lambda client: client.klines(
            "BTCUSDT",
            "1m",
            start_time=1_700_000_000_000,
            end_time=1_700_000_059_999,
            limit=1,
        ),
    )

    assert len(candles) == 1
    assert candles[0].open == Decimal("100.10")
    assert candles[0].high == Decimal("105.00")
    assert candles[0].volume == Decimal("12.25")
    assert candles[0].trade_count == 42
    assert candles[0].open_time.isoformat() == "2023-11-14T22:13:20+00:00"
    assert requests[0].url.path == "/fapi/v1/klines"
    assert requests[0].url.params["startTime"] == "1700000000000"
    assert requests[0].url.params["endTime"] == "1700000059999"
    assert requests[0].url.params["limit"] == "1"


def test_klines_reject_invalid_ohlc_and_unsorted_rows() -> None:
    valid_row = [
        1_700_000_000_000,
        "100",
        "105",
        "99",
        "103",
        "12",
        1_700_000_059_999,
        "1250",
        42,
        "5",
        "560",
        "0",
    ]
    invalid_row = [*valid_row]
    invalid_row[2] = "98"

    async def invalid_handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[invalid_row])

    with pytest.raises(BinancePayloadError, match="OHLC"):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(invalid_handler)),
            lambda client: client.klines("BTCUSDT", "1m"),
        )

    unsorted_rows = [valid_row, [*valid_row]]
    unsorted_rows[1][0] += 60_000
    unsorted_rows[1][6] += 60_000
    unsorted_rows.reverse()

    async def unsorted_handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=unsorted_rows)

    with pytest.raises(BinancePayloadError, match="strictly ordered"):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(unsorted_handler)),
            lambda client: client.klines("BTCUSDT", "1m"),
        )


def test_funding_rate_history_uses_decimal_and_utc_values() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/fapi/v1/fundingRate"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx2.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": 1_700_000_000_000,
                    "fundingRate": "0.00010000",
                    "markPrice": "30000.00",
                }
            ],
        )

    rates = run_client(
        BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
        lambda client: client.funding_rate_history("BTCUSDT", limit=1),
    )

    assert rates[0].rate == Decimal("0.00010000")
    assert rates[0].mark_price == Decimal("30000.00")
    assert rates[0].funding_time.isoformat() == "2023-11-14T22:13:20+00:00"


def test_symbol_price_tickers_parse_single_and_bulk_responses() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if "symbol" in request.url.params:
            return httpx2.Response(200, json={"symbol": "BTCUSDT", "price": "30123.45"})
        return httpx2.Response(
            200,
            json=[
                {"symbol": "BTCUSDT", "price": "30123.45"},
                {"symbol": "ETHUSDT", "price": "1800.25"},
            ],
        )

    async def read_prices(
        client: BinanceUSDmClient,
    ) -> tuple[tuple[SymbolPriceTicker, ...], tuple[SymbolPriceTicker, ...]]:
        return (
            await client.symbol_price_tickers("BTCUSDT"),
            await client.symbol_price_tickers(),
        )

    selected, all_symbols = run_client(
        BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
        read_prices,
    )

    assert selected == (SymbolPriceTicker("BTCUSDT", Decimal("30123.45")),)
    assert all_symbols == (
        SymbolPriceTicker("BTCUSDT", Decimal("30123.45")),
        SymbolPriceTicker("ETHUSDT", Decimal("1800.25")),
    )
    assert requests[0].url.path == "/fapi/v2/ticker/price"
    assert requests[0].url.params["symbol"] == "BTCUSDT"
    assert "symbol" not in requests[1].url.params


@pytest.mark.parametrize(
    ("payload", "symbol"),
    [
        ({"symbol": "BTCUSDT", "price": "0"}, "BTCUSDT"),
        ({"symbol": "ETHUSDT", "price": "1"}, "BTCUSDT"),
        (
            [
                {"symbol": "BTCUSDT", "price": "1"},
                {"symbol": "BTCUSDT", "price": "2"},
            ],
            None,
        ),
    ],
)
def test_symbol_price_tickers_reject_invalid_or_ambiguous_payloads(
    payload: object,
    symbol: str | None,
) -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    with pytest.raises(BinancePayloadError):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
            lambda client: client.symbol_price_tickers(symbol),
        )


def test_mark_prices_normalize_single_and_bulk_responses() -> None:
    requests: list[httpx2.Request] = []
    mark_payload = {
        "symbol": "BTCUSDT",
        "markPrice": "30100.25",
        "indexPrice": "30099.75",
        "estimatedSettlePrice": "0.00",
        "lastFundingRate": "-0.00010000",
        "nextFundingTime": 1_700_000_028_000,
        "interestRate": "0.00010000",
        "time": 1_700_000_000_000,
    }

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if "symbol" in request.url.params:
            return httpx2.Response(200, json=mark_payload)
        return httpx2.Response(
            200,
            json=[
                mark_payload,
                {
                    **mark_payload,
                    "symbol": "ETHUSDT",
                    "markPrice": "1800",
                    "indexPrice": "1799",
                },
            ],
        )

    async def read_prices(
        client: BinanceUSDmClient,
    ) -> tuple[tuple[MarkPrice, ...], tuple[MarkPrice, ...]]:
        return (
            await client.mark_prices("BTCUSDT"),
            await client.mark_prices(),
        )

    selected, all_symbols = run_client(
        BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
        read_prices,
    )

    assert selected[0].mark_price == Decimal("30100.25")
    assert selected[0].index_price == Decimal("30099.75")
    assert selected[0].estimated_settle_price == Decimal("0.00")
    assert selected[0].last_funding_rate == Decimal("-0.00010000")
    assert selected[0].next_funding_time.isoformat() == "2023-11-14T22:13:48+00:00"
    assert selected[0].updated_at.isoformat() == "2023-11-14T22:13:20+00:00"
    assert len(all_symbols) == 2
    assert requests[0].url.path == "/fapi/v1/premiumIndex"
    assert requests[0].url.params["symbol"] == "BTCUSDT"
    assert "symbol" not in requests[1].url.params


def test_mark_prices_reject_invalid_prices_and_duplicate_symbols() -> None:
    mark_payload = {
        "symbol": "BTCUSDT",
        "markPrice": "0",
        "indexPrice": "30000",
        "estimatedSettlePrice": "0",
        "lastFundingRate": "0.0001",
        "nextFundingTime": 1_700_000_028_000,
        "interestRate": "0.0001",
        "time": 1_700_000_000_000,
    }

    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[mark_payload, {**mark_payload, "markPrice": "1"}])

    with pytest.raises(BinancePayloadError, match="mark or index price"):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
            lambda client: client.mark_prices(),
        )

    valid_payload = {**mark_payload, "markPrice": "1"}

    async def duplicate_handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[valid_payload, valid_payload])

    with pytest.raises(BinancePayloadError, match="duplicate symbols"):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(duplicate_handler)),
            lambda client: client.mark_prices(),
        )


def test_rate_limit_honors_retry_after_and_retries_within_bound() -> None:
    attempts = 0

    async def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx2.Response(429, headers={"Retry-After": "0"})
        return httpx2.Response(200, json={"serverTime": 1_700_000_000_000})

    result = run_client(
        BinanceUSDmClient(
            transport=httpx2.MockTransport(handler),
            backoff_seconds=0,
        ),
        lambda client: client.server_time(),
    )

    assert result.year == 2023
    assert attempts == 2


def test_concurrent_requests_are_bounded() -> None:
    active = 0
    maximum_active = 0

    async def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx2.Response(200, json={"serverTime": 1_700_000_000_000})

    async def request_all() -> None:
        async with BinanceUSDmClient(transport=httpx2.MockTransport(handler)) as client:
            await asyncio.gather(*(client.server_time() for _ in range(8)))

    asyncio.run(request_all())

    assert maximum_active == 4


def test_rate_limit_and_server_errors_have_bounded_retries_and_sanitized_errors() -> None:
    attempts = 0

    async def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(503, text="internal detail must not escape")

    with pytest.raises(BinanceAPIError) as error:
        run_client(
            BinanceUSDmClient(
                transport=httpx2.MockTransport(handler),
                max_retries=2,
                backoff_seconds=0,
            ),
            lambda client: client.server_time(),
        )

    assert attempts == 3
    assert error.value.status_code == 503
    assert "internal detail" not in str(error.value)


def test_excessive_retry_after_fails_without_early_retry() -> None:
    attempts = 0

    async def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(429, headers={"Retry-After": "31"})

    with pytest.raises(BinanceAPIError) as error:
        run_client(
            BinanceUSDmClient(
                transport=httpx2.MockTransport(handler),
                max_retries=3,
                backoff_seconds=0,
            ),
            lambda client: client.server_time(),
        )

    assert attempts == 1
    assert error.value.retry_after_seconds == 31


def test_binance_ip_ban_is_not_retried() -> None:
    attempts = 0

    async def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(418)

    with pytest.raises(BinanceAPIError) as error:
        run_client(
            BinanceUSDmClient(
                transport=httpx2.MockTransport(handler),
                max_retries=4,
                backoff_seconds=0,
            ),
            lambda client: client.server_time(),
        )

    assert error.value.status_code == 418
    assert attempts == 1


def test_exchange_payload_rejects_out_of_range_timestamps() -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"serverTime": 253_402_300_800_000})

    with pytest.raises(BinancePayloadError, match="timestamp"):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
            lambda client: client.server_time(),
        )


@pytest.mark.parametrize(
    ("symbol", "interval", "limit"),
    [
        ("btcusdt", "1m", 100),
        ("BTCUSDT", "1M2", 100),
        ("BTCUSDT", "1m", 1501),
        ("BTC_USDT!", "1m", 100),
    ],
)
def test_klines_reject_invalid_request_parameters(
    symbol: str,
    interval: str,
    limit: int,
) -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        pytest.fail("invalid parameters must not make a request")

    with pytest.raises(ValueError):
        run_client(
            BinanceUSDmClient(transport=httpx2.MockTransport(handler)),
            lambda client: client.klines(symbol, interval, limit=limit),
        )
