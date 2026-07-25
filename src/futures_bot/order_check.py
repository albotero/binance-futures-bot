from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from .market_data import BinanceFuturesRESTClient
from .models import BotConfig


def place_and_cancel_test_order(
    config: BotConfig,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    if not config.symbols:
        raise ValueError("BOT_SYMBOLS must contain at least one symbol")
    if not config.api_key or not config.api_secret:
        raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET are required")
    if not config.testnet and not config.live_trading_confirmed:
        raise ValueError(
            "Mainnet order check requires BOT_LIVE_TRADING_CONFIRMED=true"
        )

    symbol = config.symbols[0]
    rest_client = client or BinanceFuturesRESTClient(
        config.api_key,
        config.api_secret,
        config.binance_base_url,
    )
    symbol_info = _find_symbol_info(
        rest_client.futures_exchange_info(), symbol)
    filters = {
        str(item.get("filterType")): item
        for item in symbol_info.get("filters", [])
        if isinstance(item, dict)
    }

    ticker = rest_client.futures_symbol_ticker(symbol)
    market_price = _positive_decimal(ticker.get("price"), "ticker price")
    tick_size = _filter_decimal(filters, "PRICE_FILTER", "tickSize")
    lot_filter = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
    if not isinstance(lot_filter, dict):
        raise ValueError(f"LOT_SIZE filter unavailable for {symbol}")
    step_size = _positive_decimal(lot_filter.get("stepSize"), "quantity step")
    minimum_quantity = _positive_decimal(
        lot_filter.get("minQty"), "minimum quantity")

    multiplier_down = Decimal("0.94")
    percent_filter = filters.get("PERCENT_PRICE")
    if isinstance(percent_filter, dict):
        raw_multiplier = percent_filter.get("multiplierDown")
        if raw_multiplier is not None:
            candidate = _positive_decimal(raw_multiplier, "price multiplier")
            if candidate < 1:
                multiplier_down = candidate
    price_ratio = (Decimal("1") + multiplier_down) / Decimal("2")
    limit_price = _round_to_step(
        market_price * price_ratio,
        tick_size,
        rounding=ROUND_DOWN,
    )

    minimum_notional = Decimal("5")
    notional_filter = filters.get("MIN_NOTIONAL")
    if isinstance(notional_filter, dict):
        raw_notional = notional_filter.get("notional")
        if raw_notional is not None:
            minimum_notional = _positive_decimal(
                raw_notional, "minimum notional")
    quantity = max(minimum_quantity, minimum_notional / limit_price)
    quantity = _round_to_step(quantity, step_size, rounding=ROUND_UP)

    client_order_id = f"bot-check-{uuid.uuid4().hex[:20]}"
    created = rest_client.futures_create_order(
        symbol=symbol,
        side="BUY",
        type="LIMIT",
        timeInForce="GTX",
        quantity=_decimal_text(quantity),
        price=_decimal_text(limit_price),
        newClientOrderId=client_order_id,
        newOrderRespType="RESULT",
    )
    raw_order_id = created.get("orderId")
    if raw_order_id is None:
        raise RuntimeError(f"Binance did not return an order ID for {symbol}")
    order_id = int(raw_order_id)
    canceled = rest_client.futures_cancel_order(symbol, order_id)

    return {
        "ok": True,
        "environment": "testnet" if config.testnet else "mainnet",
        "symbol": symbol,
        "market_price": _decimal_text(market_price),
        "limit_price": _decimal_text(limit_price),
        "quantity": _decimal_text(quantity),
        "order_id": order_id,
        "create_status": str(created.get("status", "UNKNOWN")),
        "cancel_status": str(canceled.get("status", "UNKNOWN")),
    }


def _find_symbol_info(exchange_info: dict[str, Any], symbol: str) -> dict[str, Any]:
    for item in exchange_info.get("symbols", []):
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return item
    raise ValueError(f"{symbol} is not available on the configured exchange")


def _filter_decimal(
    filters: dict[str, Any],
    filter_name: str,
    value_name: str,
) -> Decimal:
    rule = filters.get(filter_name)
    if not isinstance(rule, dict):
        raise ValueError(f"{filter_name} filter unavailable")
    return _positive_decimal(rule.get(value_name), value_name)


def _positive_decimal(raw: Any, name: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid {name}: {raw}") from exc
    if value <= 0:
        raise ValueError(f"Invalid {name}: {raw}")
    return value


def _round_to_step(value: Decimal, step: Decimal, *, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
