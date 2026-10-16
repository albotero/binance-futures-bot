from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Literal

from httpx2 import AsyncBaseTransport, AsyncClient, Limits, RequestError, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

Environment = Literal["testnet", "production"]
_BASE_URLS: dict[Environment, str] = {
    "testnet": "https://testnet.binancefuture.com",
    "production": "https://fapi.binance.com",
}
_KLINE_INTERVALS = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
)
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS = 30.0
_MAX_RETRIES = 5
_MAX_TIMEOUT_SECONDS = 120.0


class BinanceAPIError(RuntimeError):
    def __init__(
        self,
        endpoint: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        status = f"HTTP {status_code}" if status_code is not None else "transport failure"
        super().__init__(f"Binance public API request failed ({status}) at {endpoint}")


class BinancePayloadError(ValueError):
    """The public API returned a response that failed validation."""


class ClockSkewError(RuntimeError):
    """Local clock offset exceeds the configured safe threshold."""


class SymbolFilter(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    filter_type: str = Field(alias="filterType")
    min_price: Decimal | None = Field(default=None, alias="minPrice")
    max_price: Decimal | None = Field(default=None, alias="maxPrice")
    tick_size: Decimal | None = Field(default=None, alias="tickSize")
    min_quantity: Decimal | None = Field(default=None, alias="minQty")
    max_quantity: Decimal | None = Field(default=None, alias="maxQty")
    step_size: Decimal | None = Field(default=None, alias="stepSize")
    min_notional: Decimal | None = Field(default=None, alias="notional")


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    quote_asset: str
    margin_asset: str
    price_tick: Decimal
    min_price: Decimal
    max_price: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    market_quantity_step: Decimal | None
    market_min_quantity: Decimal | None
    market_max_quantity: Decimal | None
    min_notional: Decimal | None


@dataclass(frozen=True, slots=True)
class ClockSkew:
    server_time: datetime
    local_midpoint: datetime
    offset: timedelta
    round_trip: timedelta


class ExchangeSymbol(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    symbol: str
    status: str
    contract_type: str = Field(alias="contractType")
    base_asset: str = Field(alias="baseAsset")
    quote_asset: str = Field(alias="quoteAsset")
    margin_asset: str = Field(alias="marginAsset")
    price_precision: int = Field(alias="pricePrecision")
    quantity_precision: int = Field(alias="quantityPrecision")
    filters: tuple[SymbolFilter, ...]

    @field_validator("filters", mode="before")
    @classmethod
    def normalize_filters(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        validate_symbol_name(value)
        return value

    @model_validator(mode="after")
    def validate_filters(self) -> ExchangeSymbol:
        filter_types = [item.filter_type for item in self.filters]
        if len(filter_types) != len(set(filter_types)):
            raise ValueError("duplicate symbol filters")
        filters = {item.filter_type: item for item in self.filters}
        price = filters.get("PRICE_FILTER")
        lot = filters.get("LOT_SIZE")
        if price is None or lot is None:
            raise ValueError("symbol is missing required price or lot-size filters")
        if (
            price.tick_size is None
            or price.min_price is None
            or price.max_price is None
            or lot.step_size is None
            or lot.min_quantity is None
            or lot.max_quantity is None
            or (self.status == "TRADING" and (price.tick_size <= 0 or lot.step_size <= 0))
            or min(
                price.min_price,
                price.max_price,
                lot.min_quantity,
                lot.max_quantity,
            )
            < 0
            or price.max_price < price.min_price
            or lot.max_quantity < lot.min_quantity
        ):
            raise ValueError("invalid price or lot-size filters")
        for exchange_filter in self.filters:
            if any(
                value is not None and (not value.is_finite() or value < 0)
                for value in (
                    exchange_filter.min_price,
                    exchange_filter.max_price,
                    exchange_filter.tick_size,
                    exchange_filter.min_quantity,
                    exchange_filter.max_quantity,
                    exchange_filter.step_size,
                    exchange_filter.min_notional,
                )
            ):
                raise ValueError("symbol filters must contain finite non-negative values")
        return self

    def rules(self) -> SymbolRules:
        if self.status != "TRADING":
            raise ValueError("symbol is not trading; executable rules unavailable")
        filters = {item.filter_type: item for item in self.filters}
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        market_lot = filters.get("MARKET_LOT_SIZE")
        minimum_notional = filters.get("MIN_NOTIONAL")
        assert price.tick_size is not None
        assert price.min_price is not None
        assert price.max_price is not None
        assert lot.step_size is not None
        assert lot.min_quantity is not None
        assert lot.max_quantity is not None
        return SymbolRules(
            symbol=self.symbol,
            quote_asset=self.quote_asset,
            margin_asset=self.margin_asset,
            price_tick=price.tick_size,
            min_price=price.min_price,
            max_price=price.max_price,
            quantity_step=lot.step_size,
            min_quantity=lot.min_quantity,
            max_quantity=lot.max_quantity,
            market_quantity_step=market_lot.step_size if market_lot else None,
            market_min_quantity=market_lot.min_quantity if market_lot else None,
            market_max_quantity=market_lot.max_quantity if market_lot else None,
            min_notional=minimum_notional.min_notional if minimum_notional else None,
        )


class ExchangeInformation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    symbols: tuple[ExchangeSymbol, ...]

    def tradable_symbols(
        self, quote_asset: Literal["USDC", "USDT"] = "USDC"
    ) -> tuple[ExchangeSymbol, ...]:
        if quote_asset not in ("USDC", "USDT"):
            raise ValueError("quote asset must be USDC or USDT")
        return tuple(
            symbol
            for symbol in self.symbols
            if symbol.status == "TRADING"
            and symbol.contract_type == "PERPETUAL"
            and symbol.quote_asset == quote_asset
            and symbol.margin_asset == quote_asset
        )


@dataclass(frozen=True, slots=True)
class Kline:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_volume: Decimal
    taker_buy_quote_volume: Decimal


@dataclass(frozen=True, slots=True)
class FundingRate:
    symbol: str
    funding_time: datetime
    rate: Decimal
    mark_price: Decimal


@dataclass(frozen=True, slots=True)
class SymbolPriceTicker:
    symbol: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    mark_price: Decimal
    index_price: Decimal
    estimated_settle_price: Decimal
    last_funding_rate: Decimal
    next_funding_time: datetime
    interest_rate: Decimal
    updated_at: datetime


class BinanceUSDmClient:
    """Bounded, public-only REST client for Binance USDⓈ-M Futures."""

    def __init__(
        self,
        environment: Environment = "testnet",
        *,
        transport: AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(environment, str) or environment not in _BASE_URLS:
            raise ValueError("environment must be testnet or production")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds must be between 0 and 120 seconds")
        if type(max_retries) is not int or not 0 <= max_retries <= _MAX_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {_MAX_RETRIES}")
        if (
            isinstance(backoff_seconds, bool)
            or not isinstance(backoff_seconds, (int, float))
            or not 0 <= backoff_seconds <= _MAX_RETRY_DELAY_SECONDS
            or not math.isfinite(backoff_seconds)
        ):
            raise ValueError("backoff_seconds must be between 0 and 30 seconds")

        self.base_url = _BASE_URLS[environment]
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._request_slots = asyncio.Semaphore(4)
        self._client = AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            limits=Limits(max_connections=4, max_keepalive_connections=4),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> BinanceUSDmClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def server_time(self) -> datetime:
        payload = await self._get("/fapi/v1/time")
        if not isinstance(payload, dict):
            raise BinancePayloadError("invalid server-time response")
        return _utc_from_milliseconds(payload.get("serverTime"))

    async def clock_skew(self, max_skew_seconds: float = 1.0) -> ClockSkew:
        if (
            isinstance(max_skew_seconds, bool)
            or not isinstance(max_skew_seconds, (int, float))
            or not math.isfinite(max_skew_seconds)
            or not 0 < max_skew_seconds <= 30
        ):
            raise ValueError("max_skew_seconds must be between 0 and 30 seconds")

        local_before = self._wall_clock()
        if local_before.tzinfo is None or local_before.utcoffset() is None:
            raise ValueError("wall clock must return a timezone-aware datetime")
        local_before = local_before.astimezone(UTC)
        monotonic_before = asyncio.get_running_loop().time()
        server = await self.server_time()
        monotonic_after = asyncio.get_running_loop().time()
        local_after = self._wall_clock()
        if local_after.tzinfo is None or local_after.utcoffset() is None:
            raise ValueError("wall clock must return a timezone-aware datetime")
        local_after = local_after.astimezone(UTC)
        round_trip = monotonic_after - monotonic_before
        local_midpoint = local_before + (local_after - local_before) / 2
        skew = ClockSkew(
            server_time=server,
            local_midpoint=local_midpoint,
            offset=server - local_midpoint,
            round_trip=timedelta(seconds=round_trip),
        )
        if abs(skew.offset.total_seconds()) > max_skew_seconds:
            raise ClockSkewError(
                f"local clock differs from Binance by {skew.offset.total_seconds():.3f}s"
            )
        return skew

    async def exchange_information(self) -> ExchangeInformation:
        payload = await self._get("/fapi/v1/exchangeInfo")
        try:
            information = ExchangeInformation.model_validate(payload)
        except ValidationError as exc:
            raise BinancePayloadError("invalid exchange-information response") from exc
        symbols = [item.symbol for item in information.symbols]
        if len(symbols) != len(set(symbols)):
            raise BinancePayloadError("duplicate symbols in exchange-information response")
        return information

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> tuple[Kline, ...]:
        validate_symbol_name(symbol)
        if not isinstance(interval, str) or interval not in _KLINE_INTERVALS:
            raise ValueError("unsupported kline interval")
        _validate_time_range(start_time, end_time)
        _validate_limit(limit, 1500, "kline")

        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        _add_time_params(params, start_time, end_time)
        payload = await self._get("/fapi/v1/klines", params)
        if not isinstance(payload, list):
            raise BinancePayloadError("invalid kline response")

        candles = tuple(_parse_kline(row) for row in payload)
        open_times = [candle.open_time for candle in candles]
        if open_times != sorted(set(open_times)):
            raise BinancePayloadError("kline response is not strictly ordered")
        return candles

    async def funding_rate_history(
        self,
        symbol: str | None = None,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> tuple[FundingRate, ...]:
        if symbol is not None:
            validate_symbol_name(symbol)
        _validate_time_range(start_time, end_time)
        _validate_limit(limit, 1000, "funding-rate")

        params: dict[str, str | int] = {"limit": limit}
        if symbol is not None:
            params["symbol"] = symbol
        _add_time_params(params, start_time, end_time)
        payload = await self._get("/fapi/v1/fundingRate", params)
        if not isinstance(payload, list):
            raise BinancePayloadError("invalid funding-rate response")

        rates = tuple(_parse_funding_rate(item) for item in payload)
        funding_times = [item.funding_time for item in rates]
        if funding_times != sorted(funding_times):
            raise BinancePayloadError("funding-rate response is not ordered")
        return rates

    async def symbol_price_tickers(
        self,
        symbol: str | None = None,
    ) -> tuple[SymbolPriceTicker, ...]:
        if symbol is not None:
            validate_symbol_name(symbol)

        params: dict[str, str | int] | None = {"symbol": symbol} if symbol is not None else None
        payload = await self._get("/fapi/v2/ticker/price", params)
        rows = _response_rows(payload, symbol, "symbol-price ticker")
        tickers = tuple(_parse_symbol_price_ticker(row) for row in rows)
        symbols = [ticker.symbol for ticker in tickers]
        if len(symbols) != len(set(symbols)):
            raise BinancePayloadError("duplicate symbols in symbol-price response")
        if symbol is not None and symbols != [symbol]:
            raise BinancePayloadError("symbol-price response does not match requested symbol")
        return tickers

    async def mark_prices(self, symbol: str | None = None) -> tuple[MarkPrice, ...]:
        if symbol is not None:
            validate_symbol_name(symbol)

        params: dict[str, str | int] | None = {"symbol": symbol} if symbol is not None else None
        payload = await self._get("/fapi/v1/premiumIndex", params)
        rows = _response_rows(payload, symbol, "mark-price")
        prices = tuple(_parse_mark_price(row) for row in rows)
        symbols = [price.symbol for price in prices]
        if len(symbols) != len(set(symbols)):
            raise BinancePayloadError("duplicate symbols in mark-price response")
        if symbol is not None and symbols != [symbol]:
            raise BinancePayloadError("mark-price response does not match requested symbol")
        return prices

    async def _get(
        self,
        endpoint: str,
        params: dict[str, str | int] | None = None,
    ) -> object:
        for attempt in range(self._max_retries + 1):
            try:
                async with self._request_slots:
                    response = await self._client.get(endpoint, params=params)
            except RequestError as exc:
                if attempt == self._max_retries:
                    raise BinanceAPIError(endpoint) from exc
                await asyncio.sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 418:
                raise BinanceAPIError(endpoint, status_code=status)
            if status in _RETRYABLE_STATUSES:
                retry_after = _retry_after_seconds(response)
                if attempt == self._max_retries:
                    raise BinanceAPIError(
                        endpoint,
                        status_code=status,
                        retry_after_seconds=retry_after,
                    )
                delay = (
                    retry_after
                    if status == 429 and retry_after is not None
                    else self._backoff(attempt)
                )
                if delay > _MAX_RETRY_DELAY_SECONDS:
                    raise BinanceAPIError(
                        endpoint,
                        status_code=status,
                        retry_after_seconds=delay,
                    )
                await asyncio.sleep(delay)
                continue
            if not 200 <= status < 300:
                raise BinanceAPIError(endpoint, status_code=status)

            try:
                return response.json()
            except ValueError as exc:
                raise BinancePayloadError(f"invalid JSON response at {endpoint}") from exc

        raise RuntimeError("unreachable retry state")

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_seconds * (2**attempt), _MAX_RETRY_DELAY_SECONDS)


def validate_symbol_name(symbol: str) -> None:
    if (
        not isinstance(symbol, str)
        or len(symbol) < 2
        or len(symbol) > 32
        or not all(character.isalnum() or character == "_" for character in symbol)
    ):
        raise ValueError("symbol must contain 2-32 letters, digits, or underscores")
    if symbol != symbol.upper():
        raise ValueError("symbol must be uppercase")


def _validate_time_range(start_time: int | None, end_time: int | None) -> None:
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a non-negative millisecond timestamp")
    if start_time is not None and end_time is not None and end_time < start_time:
        raise ValueError("end_time cannot precede start_time")


def _validate_limit(limit: int, maximum: int, name: str) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"{name} limit must be between 1 and {maximum}")


def _add_time_params(
    params: dict[str, str | int],
    start_time: int | None,
    end_time: int | None,
) -> None:
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time


def _decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise BinancePayloadError(f"invalid {field_name} in exchange response")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BinancePayloadError(f"invalid {field_name} in exchange response") from exc
    if not parsed.is_finite():
        raise BinancePayloadError(f"invalid {field_name} in exchange response")
    return parsed


def _utc_from_milliseconds(value: object) -> datetime:
    if type(value) is not int or not 0 <= value <= 253_402_300_799_999:
        raise BinancePayloadError("invalid millisecond timestamp in exchange response")
    return datetime.fromtimestamp(value // 1000, UTC).replace(microsecond=value % 1000 * 1000)


def _parse_kline(row: object) -> Kline:
    if not isinstance(row, list) or len(row) != 12:
        raise BinancePayloadError("invalid kline row in exchange response")

    open_time = _utc_from_milliseconds(row[0])
    open_price = _decimal(row[1], "open price")
    high_price = _decimal(row[2], "high price")
    low_price = _decimal(row[3], "low price")
    close_price = _decimal(row[4], "close price")
    volume = _decimal(row[5], "volume")
    close_time = _utc_from_milliseconds(row[6])
    quote_volume = _decimal(row[7], "quote volume")
    if type(row[8]) is not int or row[8] < 0:
        raise BinancePayloadError("invalid trade count in exchange response")
    taker_buy_volume = _decimal(row[9], "taker-buy volume")
    taker_buy_quote_volume = _decimal(row[10], "taker-buy quote volume")
    _decimal(row[11], "unused kline field")

    if (
        close_time <= open_time
        or min(open_price, high_price, low_price, close_price) <= 0
        or high_price < max(open_price, low_price, close_price)
        or low_price > min(open_price, high_price, close_price)
        or min(volume, quote_volume, taker_buy_volume, taker_buy_quote_volume) < 0
    ):
        raise BinancePayloadError("invalid OHLC or volume values in exchange response")

    return Kline(
        open_time=open_time,
        close_time=close_time,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        quote_volume=quote_volume,
        trade_count=row[8],
        taker_buy_volume=taker_buy_volume,
        taker_buy_quote_volume=taker_buy_quote_volume,
    )


def _parse_funding_rate(item: object) -> FundingRate:
    if not isinstance(item, dict):
        raise BinancePayloadError("invalid funding-rate row in exchange response")
    symbol = item.get("symbol")
    if not isinstance(symbol, str):
        raise BinancePayloadError("invalid funding-rate symbol in exchange response")
    try:
        validate_symbol_name(symbol)
    except ValueError as exc:
        raise BinancePayloadError("invalid funding-rate symbol in exchange response") from exc
    rate = _decimal(item.get("fundingRate"), "funding rate")
    mark_price = _decimal(item.get("markPrice"), "mark price")
    if mark_price <= 0:
        raise BinancePayloadError("invalid mark price in exchange response")
    return FundingRate(
        symbol=symbol,
        funding_time=_utc_from_milliseconds(item.get("fundingTime")),
        rate=rate,
        mark_price=mark_price,
    )


def _response_rows(payload: object, symbol: str | None, name: str) -> tuple[object, ...]:
    if symbol is not None:
        if not isinstance(payload, dict):
            raise BinancePayloadError(f"invalid {name} response")
        return (payload,)
    if not isinstance(payload, list):
        raise BinancePayloadError(f"invalid {name} response")
    return tuple(payload)


def _parse_symbol_price_ticker(item: object) -> SymbolPriceTicker:
    if not isinstance(item, dict):
        raise BinancePayloadError("invalid symbol-price row in exchange response")
    symbol = _response_symbol(item.get("symbol"), "symbol-price")
    price = _decimal(item.get("price"), "symbol price")
    if price <= 0:
        raise BinancePayloadError("invalid symbol price in exchange response")
    return SymbolPriceTicker(symbol=symbol, price=price)


def _parse_mark_price(item: object) -> MarkPrice:
    if not isinstance(item, dict):
        raise BinancePayloadError("invalid mark-price row in exchange response")
    symbol = _response_symbol(item.get("symbol"), "mark-price")
    mark_price = _decimal(item.get("markPrice"), "mark price")
    index_price = _decimal(item.get("indexPrice"), "index price")
    estimated_settle_price = _decimal(item.get("estimatedSettlePrice"), "estimated settle price")
    last_funding_rate = _decimal(item.get("lastFundingRate"), "last funding rate")
    interest_rate = _decimal(item.get("interestRate"), "interest rate")
    if min(mark_price, index_price) <= 0 or estimated_settle_price < 0:
        raise BinancePayloadError("invalid mark or index price in exchange response")
    if interest_rate < 0:
        raise BinancePayloadError("invalid interest rate in exchange response")
    return MarkPrice(
        symbol=symbol,
        mark_price=mark_price,
        index_price=index_price,
        estimated_settle_price=estimated_settle_price,
        last_funding_rate=last_funding_rate,
        next_funding_time=_utc_from_milliseconds(item.get("nextFundingTime")),
        interest_rate=interest_rate,
        updated_at=_utc_from_milliseconds(item.get("time")),
    )


def _response_symbol(value: object, endpoint: str) -> str:
    if not isinstance(value, str):
        raise BinancePayloadError(f"invalid {endpoint} symbol in exchange response")
    try:
        validate_symbol_name(value)
    except ValueError as exc:
        raise BinancePayloadError(f"invalid {endpoint} symbol in exchange response") from exc
    return value


def _retry_after_seconds(response: Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds)
