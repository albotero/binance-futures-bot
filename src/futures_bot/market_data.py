from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(slots=True)
class BinanceFuturesRESTClient:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://fapi.binance.com"

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        payload = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        if signed:
            payload["timestamp"] = int(time.time() * 1000)
            payload["recvWindow"] = 5000
        query = self._encode_params(payload)
        if signed:
            signature = hmac.new(self.api_secret.encode(),
                                 query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = Request(url, data=None if method ==
                          "GET" else b"", method=method, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            raw_body = ""
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                raw_body = ""

            detail = raw_body.strip() or str(exc.reason)
            try:
                payload = json.loads(raw_body) if raw_body else {}
                if isinstance(payload, dict):
                    code = payload.get("code")
                    msg = payload.get("msg")
                    if code is not None and msg:
                        detail = f"{msg} (code {code})"
            except json.JSONDecodeError:
                pass

            if exc.code == 401:
                detail = (
                    f"{detail}. Check API key/secret, Futures trading permission, "
                    "IP whitelist, and testnet/mainnet key alignment."
                )

            raise RuntimeError(
                f"Binance API {method} {path} failed ({exc.code}): {detail}") from exc

    @staticmethod
    def _encode_params(params: dict[str, Any]) -> str:
        normalized: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                normalized[key] = str(value).lower()
            else:
                normalized[key] = value
        return urlencode(normalized, doseq=True)

    def futures_exchange_info(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def futures_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        return self._request(
            "GET",
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "startTime": start_time,
                "endTime": end_time,
            },
        )

    def futures_symbol_ticker(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})

    def futures_change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def futures_create_order(self, **params: Any) -> dict[str, Any]:
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def futures_place_algo_order(self, **params: Any) -> dict[str, Any]:
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def futures_get_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        return self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    def futures_cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    def futures_cancel_algo_order(self, symbol: str, algo_id: int) -> dict[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {"symbol": symbol, "algoId": algo_id},
            signed=True,
        )


@dataclass(slots=True)
class BinanceMarketData:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://fapi.binance.com"
    client: BinanceFuturesRESTClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = BinanceFuturesRESTClient(
            self.api_key, self.api_secret, self.base_url)

    def list_symbols(self, quote_asset: str = "USDT") -> list[str]:
        exchange_info = self.client.futures_exchange_info()
        symbols: list[str] = []
        for item in exchange_info["symbols"]:
            if item.get("status") != "TRADING":
                continue
            if item.get("quoteAsset") != quote_asset:
                continue
            symbols.append(item["symbol"])
        return sorted(set(symbols))

    def fetch_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, float]]:
        rows = self.client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        candles: list[dict[str, float]] = []
        for row in rows:
            candles.append(
                {
                    "open_time": float(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": float(row[6]),
                }
            )
        return candles

    def latest_price(self, symbol: str) -> float:
        return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])
