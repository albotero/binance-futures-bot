"""Read-only, signed Binance production leverage-bracket lookup."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode

import httpx2 as httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from futures_bot.exchange.binance_usdm import validate_symbol_name


class LeverageLookupError(RuntimeError):
    """Sanitized operator-facing error; never includes signed URLs or keys."""


class LeverageBracket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_leverage: StrictInt = Field(ge=1)
    notional_floor: Decimal = Field(ge=0, allow_inf_nan=False)
    notional_cap: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> LeverageBracket:
        if self.notional_cap <= self.notional_floor:
            raise ValueError("bracket cap must exceed its floor")
        return self


class LeverageSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    environment: str = Field(default="production", pattern="^production$")
    fetched_at: AwareDatetime
    brackets: tuple[LeverageBracket, ...] = Field(min_length=1)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        validate_symbol_name(value)
        return value

    @model_validator(mode="after")
    def contiguous(self) -> LeverageSchedule:
        floor = Decimal(0)
        leverage = self.brackets[0].initial_leverage
        for bracket in self.brackets:
            if bracket.notional_floor != floor or bracket.initial_leverage > leverage:
                raise ValueError("brackets must be contiguous with non-increasing leverage")
            floor = bracket.notional_cap
            leverage = bracket.initial_leverage
        return self

    @property
    def maximum_leverage(self) -> int:
        return self.brackets[0].initial_leverage

    def notional_limit(self, leverage: int) -> Decimal:
        eligible = [b.notional_cap for b in self.brackets if b.initial_leverage >= leverage]
        if not eligible:
            raise ValueError(f"{self.symbol} leverage exceeds verified Binance maximum")
        return max(eligible)


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_key: SecretStr
    api_secret: SecretStr


def _credentials(path: Path) -> _Credentials:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid not in (0, os.getuid())
                or metadata.st_size > 16384
            ):
                raise LeverageLookupError("Binance credential file must be private and owner-only")
            credentials = _Credentials.model_validate(json.load(stream))
            if not all(
                value.get_secret_value().strip()
                for value in (credentials.api_key, credentials.api_secret)
            ):
                raise LeverageLookupError("Binance read-only credentials are empty")
            return credentials
    except (OSError, ValueError, ValidationError):
        raise LeverageLookupError(
            "Binance read-only credential file is missing or invalid"
        ) from None


class LeverageProvider:
    """Only GET time and leverageBracket; no order/account mutation interface."""

    def __init__(
        self, credentials_file: Path, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.credentials_file = credentials_file
        self.transport = transport
        self._cache: tuple[float, tuple[LeverageSchedule, ...]] | None = None
        self._lock = asyncio.Lock()

    async def schedules(self) -> tuple[LeverageSchedule, ...]:
        async with self._lock:
            if self._cache is not None and monotonic() - self._cache[0] < 60:
                return self._cache[1]
            credentials = _credentials(self.credentials_file)
            try:
                async with httpx.AsyncClient(
                    base_url="https://fapi.binance.com",
                    timeout=10,
                    transport=self.transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    started = monotonic()
                    clock = await client.get("/fapi/v1/time")
                    if clock.status_code != 200:
                        raise LeverageLookupError("Binance server time is unavailable")
                    timestamp = clock.json()["serverTime"]
                    if type(timestamp) is not int or timestamp <= 0 or monotonic() - started > 5:
                        raise LeverageLookupError("Binance server time is invalid or delayed")
                    query = urlencode({"timestamp": timestamp, "recvWindow": 5000})
                    signature = hmac.new(
                        credentials.api_secret.get_secret_value().encode(),
                        query.encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    response = await client.get(
                        f"/fapi/v1/leverageBracket?{query}&signature={signature}",
                        headers={"X-MBX-APIKEY": credentials.api_key.get_secret_value()},
                    )
                    if response.status_code != 200:
                        raise LeverageLookupError(
                            f"Binance leverage verification refused (HTTP {response.status_code}); "
                            "check API-key permissions, IP allowlist and futures access"
                        )
                    payload = response.json(parse_float=Decimal)
                    if not isinstance(payload, list) or not payload:
                        raise LeverageLookupError("Binance returned no leverage brackets")
                    now = datetime.now(UTC)
                    schedules = tuple(
                        LeverageSchedule(
                            symbol=item["symbol"],
                            fetched_at=now,
                            brackets=tuple(
                                LeverageBracket(
                                    initial_leverage=bracket["initialLeverage"],
                                    notional_floor=bracket["notionalFloor"],
                                    notional_cap=bracket["notionalCap"],
                                )
                                for bracket in item["brackets"]
                            ),
                        )
                        for item in payload
                    )
                    if len({s.symbol for s in schedules}) != len(schedules):
                        raise LeverageLookupError("Binance returned duplicate leverage schedules")
            except (httpx.RequestError, ValueError, KeyError, TypeError):
                raise LeverageLookupError(
                    "Binance leverage verification is unavailable or invalid; retry"
                ) from None
            self._cache = (monotonic(), schedules)
            return schedules
