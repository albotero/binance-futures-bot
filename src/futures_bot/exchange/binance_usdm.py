from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Literal

from httpx2 import AsyncBaseTransport, AsyncClient, Limits, RequestError, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        _validate_symbol(value)
        return value


class ExchangeInformation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    symbols: tuple[ExchangeSymbol, ...]


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
        _validate_symbol(symbol)
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
            _validate_symbol(symbol)
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


def _validate_symbol(symbol: str) -> None:
    if (
        not isinstance(symbol, str)
        or len(symbol) < 2
        or len(symbol) > 32
        or not symbol.isascii()
        or not all(character.isalnum() or character == "_" for character in symbol)
    ):
        raise ValueError("symbol must contain only ASCII letters, digits, or underscores")
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
        _validate_symbol(symbol)
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
