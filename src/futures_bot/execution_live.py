from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any

from .execution import BaseExecution
from .market_data import BinanceFuturesRESTClient
from .models import Position, Side, TradeStatus, iso


BINANCE_TRAILING_CALLBACK_MIN = 0.1
BINANCE_TRAILING_CALLBACK_MAX = 5.0


@dataclass(slots=True)
class ExchangeFillSummary:
    symbol: str
    order_id: int
    side: str
    quantity: float
    average_price: float
    realized_pnl: float
    fill_time_ms: int
    fill_time: str


@dataclass(slots=True)
class BinanceFuturesExecution(BaseExecution):
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://fapi.binance.com"
    live_protection_mode: str = "local_and_exchange"
    client: BinanceFuturesRESTClient = field(init=False)
    protective_orders: dict[str, list[int]] = field(
        default_factory=dict, init=False)
    symbol_filters: dict[str, tuple[float | None, float | None]] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        self.client = BinanceFuturesRESTClient(
            self.api_key, self.api_secret, self.base_url)

    def open_position(self, position: Position) -> Position:
        requested_entry = position.entry_price
        side = "BUY" if position.side == Side.LONG else "SELL"
        self.client.futures_change_leverage(
            symbol=position.symbol, leverage=position.leverage)
        order = self.client.futures_create_order(
            symbol=position.symbol,
            side=side,
            type="MARKET",
            quantity=self._format_quantity(position.symbol, position.quantity),
            newOrderRespType="RESULT",
        )
        order_id = _extract_order_id(order)
        fill = self._exchange_fill_for_order(
            position.symbol,
            order_id,
            order_payload=order,
            fallback_price=position.entry_price,
        )
        fill_price = fill.average_price if fill else self._exchange_reference_price(
            position.symbol)
        position.entry_order_id = order_id
        if fill and fill.fill_time:
            position.opened_at = fill.fill_time
        position.entry_price = fill_price
        position.mark(fill_price)
        self._realign_protective_prices(
            position,
            requested_entry=requested_entry,
            fill_price=fill_price,
        )
        if self._uses_exchange_protection():
            try:
                self.protective_orders[position.symbol] = self._place_protective_orders(
                    position)
            except Exception as exc:  # noqa: BLE001
                self._emergency_flatten(position)
                raise RuntimeError(
                    f"Failed to place protective TP/SL orders for {position.symbol}: {exc}") from exc
        else:
            self.protective_orders[position.symbol] = []
        self.positions[position.symbol] = position
        return position

    def close_position(self, symbol: str, price: float, reason: str = "") -> Position | None:
        position = self.positions.get(symbol)
        if not position:
            return None
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        order = self.client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=self._format_quantity(symbol, position.quantity),
            reduceOnly=True,
            newOrderRespType="RESULT",
        )
        order_id = _extract_order_id(order)
        fill = self._exchange_fill_for_order(
            symbol,
            order_id,
            order_payload=order,
            fallback_price=price,
        )
        fill_price = fill.average_price if fill else self._exchange_reference_price(
            symbol)
        self._cancel_protective_orders(symbol)
        self._cancel_exchange_algo_orders(symbol)
        self.positions.pop(symbol, None)
        position.exit_order_id = order_id
        if fill and fill.fill_time:
            position.updated_at = fill.fill_time
        self._finalize_closed_position(
            position,
            exit_price=fill_price,
            reason=reason or TradeStatus.CLOSED.value,
        )
        return position

    def close_position_from_exchange(
        self,
        symbol: str,
        price: float,
        reason: str = "",
        *,
        fill_time: str | None = None,
        order_id: int | None = None,
    ) -> Position | None:
        position = self.positions.get(symbol)
        if not position:
            return None
        self._cancel_protective_orders(symbol)
        self._cancel_exchange_algo_orders(symbol)
        self.positions.pop(symbol, None)
        position.exit_order_id = order_id
        if fill_time:
            position.updated_at = fill_time
        self._finalize_closed_position(
            position,
            exit_price=price,
            reason=reason or TradeStatus.CLOSED.value,
        )
        return position

    def exchange_position_snapshot(self, symbol: str) -> tuple[float, float]:
        try:
            rows = self.client.futures_position_risk(symbol)
        except Exception:  # noqa: BLE001
            return 0.0, 0.0
        for item in rows:
            if str(item.get("symbol", "")).upper() != symbol.upper():
                continue
            qty = _safe_float(item.get("positionAmt"))
            mark_price = _safe_float(item.get("markPrice"))
            return qty, mark_price
        return 0.0, 0.0

    def exchange_fill_history(self, symbol: str, *, start_time_ms: int | None = None) -> list[ExchangeFillSummary]:
        try:
            rows = self.client.futures_user_trades(
                symbol,
                limit=1000,
                start_time=start_time_ms,
            )
        except AttributeError:
            rows = []
        return _group_exchange_fills(rows)

    def find_exchange_trade_fills(
        self,
        symbol: str,
        *,
        trade_side: str,
        quantity: float,
        opened_at: str,
    ) -> tuple[ExchangeFillSummary | None, ExchangeFillSummary | None]:
        start_ms = _iso_to_ms(opened_at)
        fills = self.exchange_fill_history(symbol, start_time_ms=start_ms)
        return _match_trade_to_fills(fills, trade_side=trade_side, quantity=quantity, opened_at_ms=start_ms)

    def _finalize_closed_position(self, position: Position, exit_price: float, reason: str) -> float:
        position.mark(exit_price)
        if position.side == Side.LONG:
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity
        position.realized_pnl = pnl
        position.status = TradeStatus(reason)
        position.close_reason = reason
        self.realized_pnl += pnl
        self.balance += pnl
        return pnl

    def _filled_price_from_order(self, symbol: str, order: Any, fallback_price: float) -> float:
        direct = _extract_fill_price(order)
        if direct is not None and direct > 0:
            return direct
        order_id = _extract_order_id(order)
        if order_id is None:
            return fallback_price
        try:
            fetched = self.client.futures_get_order(
                symbol=symbol, order_id=order_id)
        except Exception:  # noqa: BLE001
            return fallback_price
        resolved = _extract_fill_price(fetched)
        if resolved is not None and resolved > 0:
            return resolved
        return fallback_price

    def _exchange_fill_for_order(
        self,
        symbol: str,
        order_id: int | None,
        *,
        order_payload: Any,
        fallback_price: float,
    ) -> ExchangeFillSummary | None:
        direct_price = _extract_fill_price(order_payload)
        if direct_price is not None and direct_price > 0:
            return ExchangeFillSummary(
                symbol=symbol,
                order_id=order_id or 0,
                side="",
                quantity=0.0,
                average_price=direct_price,
                realized_pnl=0.0,
                fill_time_ms=0,
                fill_time="",
            )
        if order_id is not None:
            fills = self.exchange_fill_history(symbol)
            for fill in fills:
                if fill.order_id == order_id:
                    return fill
        fetched_price = self._filled_price_from_order(
            symbol, {"orderId": order_id}, fallback_price)
        if fetched_price <= 0:
            fetched_price = self._exchange_reference_price(symbol)
        if fetched_price <= 0:
            return None
        return ExchangeFillSummary(
            symbol=symbol,
            order_id=order_id or 0,
            side="",
            quantity=0.0,
            average_price=fetched_price,
            realized_pnl=0.0,
            fill_time_ms=0,
            fill_time="",
        )

    def _exchange_reference_price(self, symbol: str) -> float:
        try:
            return float(self.client.futures_symbol_ticker(symbol=symbol).get("price") or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _place_protective_orders(self, position: Position) -> list[int]:
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        # Round triggers directionally to keep them on the intended side.
        stop_price = self._format_protective_price(
            position.symbol,
            position.stop_loss_price,
            round_down=(position.side == Side.LONG),
        )
        take_profit_price = self._format_protective_price(
            position.symbol,
            position.take_profit_price,
            round_down=(position.side == Side.SHORT),
        )

        stop_order = self.client.futures_place_algo_order(
            algoType="CONDITIONAL",
            symbol=position.symbol,
            side=close_side,
            type="STOP_MARKET",
            quantity=self._format_quantity(position.symbol, position.quantity),
            triggerPrice=stop_price,
            reduceOnly=True,
            workingType="MARK_PRICE",
        )
        take_profit_order = self.client.futures_place_algo_order(
            algoType="CONDITIONAL",
            symbol=position.symbol,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            quantity=self._format_quantity(position.symbol, position.quantity),
            triggerPrice=take_profit_price,
            reduceOnly=True,
            workingType="MARK_PRICE",
        )

        trailing_order: dict[str, Any] | None = None
        callback_rate = _trailing_callback_rate(position)
        if callback_rate is not None and not position.trailing_stage_enabled:
            try:
                trailing_order = self.client.futures_place_algo_order(
                    algoType="CONDITIONAL",
                    symbol=position.symbol,
                    side=close_side,
                    type="TRAILING_STOP_MARKET",
                    quantity=self._format_quantity(
                        position.symbol, position.quantity),
                    activatePrice=self._format_protective_price(
                        position.symbol,
                        position.trailing_stop_price,
                        round_down=(position.side == Side.LONG),
                    ),
                    callbackRate=callback_rate,
                    reduceOnly=True,
                    workingType="MARK_PRICE",
                )
            except Exception:  # noqa: BLE001
                # Keep STOP/TP protection even if trailing order fails for a symbol.
                trailing_order = None

        order_ids: list[int] = []
        for order in (stop_order, take_profit_order, trailing_order):
            order_id = _extract_order_id(order)
            if order_id is not None:
                order_ids.append(order_id)
        return order_ids

    def _realign_protective_prices(self, position: Position, requested_entry: float, fill_price: float) -> None:
        if requested_entry <= 0 or fill_price <= 0:
            return

        delta = fill_price - requested_entry
        position.stop_loss_price += delta
        position.take_profit_price += delta
        if position.trailing_stop_price:
            position.trailing_stop_price += delta

        # Keep protective levels on the correct side of entry to avoid invalid/immediate triggers.
        buffer = fill_price * 1e-6
        if position.side == Side.LONG:
            position.stop_loss_price = min(
                position.stop_loss_price, fill_price - buffer)
            position.take_profit_price = max(
                position.take_profit_price, fill_price + buffer)
            if position.trailing_stop_price:
                position.trailing_stop_price = min(
                    position.trailing_stop_price, fill_price - buffer)
        else:
            position.stop_loss_price = max(
                position.stop_loss_price, fill_price + buffer)
            position.take_profit_price = min(
                position.take_profit_price, fill_price - buffer)
            if position.trailing_stop_price:
                position.trailing_stop_price = max(
                    position.trailing_stop_price, fill_price + buffer)

    def _cancel_protective_orders(self, symbol: str) -> None:
        order_ids = self.protective_orders.pop(symbol, [])
        for order_id in order_ids:
            try:
                self.client.futures_cancel_algo_order(
                    symbol=symbol, algo_id=order_id)
            except Exception:  # noqa: BLE001
                # Orders may already be filled/canceled at exchange side.
                continue

    def _cancel_exchange_algo_orders(self, symbol: str) -> None:
        try:
            open_orders = self.client.futures_open_algo_orders(symbol)
        except Exception:  # noqa: BLE001
            return
        for order in open_orders:
            order_id = _extract_order_id(order)
            if order_id is None:
                continue
            try:
                self.client.futures_cancel_algo_order(
                    symbol=symbol, algo_id=order_id)
            except Exception:  # noqa: BLE001
                continue

    def _emergency_flatten(self, position: Position) -> None:
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        try:
            self.client.futures_create_order(
                symbol=position.symbol,
                side=close_side,
                type="MARKET",
                quantity=self._format_quantity(
                    position.symbol, position.quantity),
                reduceOnly=True,
            )
        except Exception:  # noqa: BLE001
            pass

    def _format_protective_price(self, symbol: str, price: float, round_down: bool) -> float:
        tick_size, _ = self._get_symbol_filters(symbol)
        if tick_size and tick_size > 0:
            return _quantize_to_step(
                price,
                tick_size,
                mode="down" if round_down else "up",
            )
        quantum = Decimal("0.000001")
        rounded = Decimal(str(price)).quantize(
            quantum,
            rounding=ROUND_DOWN if round_down else ROUND_UP,
        )
        return float(rounded)

    def _format_quantity(self, symbol: str, quantity: float) -> float:
        _, qty_step = self._get_symbol_filters(symbol)
        if qty_step and qty_step > 0:
            quantized = _quantize_to_step(quantity, qty_step, mode="down")
            if quantized > 0:
                return quantized
        # Fallback should never round up; keep quantity conservative.
        return _quantize_to_step(quantity, 0.00001, mode="down")

    def _uses_exchange_protection(self) -> bool:
        return self.live_protection_mode.strip().lower() == "local_and_exchange"

    def _get_symbol_filters(self, symbol: str) -> tuple[float | None, float | None]:
        cached = self.symbol_filters.get(symbol)
        if cached is not None:
            return cached

        tick_size: float | None = None
        qty_step: float | None = None
        try:
            exchange_info = self.client.futures_exchange_info()
            for item in exchange_info.get("symbols", []):
                if item.get("symbol") != symbol:
                    continue
                for rule in item.get("filters", []):
                    rule_type = rule.get("filterType")
                    if rule_type == "PRICE_FILTER":
                        tick_size = _as_positive_float(rule.get("tickSize"))
                    elif rule_type == "MARKET_LOT_SIZE":
                        qty_step = _as_positive_float(rule.get("stepSize"))
                    elif rule_type == "LOT_SIZE" and not qty_step:
                        qty_step = _as_positive_float(rule.get("stepSize"))
                break
        except Exception:  # noqa: BLE001
            tick_size = None
            qty_step = None

        self.symbol_filters[symbol] = (tick_size, qty_step)
        return tick_size, qty_step


def _extract_order_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("orderId")
    if raw is None:
        raw = payload.get("algoId")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _trailing_callback_rate(position: Position) -> float | None:
    if position.current_price <= 0:
        return None
    if position.side == Side.LONG:
        distance_pct = (position.current_price -
                        position.trailing_stop_price) / position.current_price * 100
    else:
        distance_pct = (position.trailing_stop_price -
                        position.current_price) / position.current_price * 100
    if distance_pct <= 0:
        return None
    return round(
        min(
            max(distance_pct, BINANCE_TRAILING_CALLBACK_MIN),
            BINANCE_TRAILING_CALLBACK_MAX,
        ),
        2,
    )


def _extract_fill_price(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None

    avg = payload.get("avgPrice")
    try:
        avg_value = float(avg)
    except (TypeError, ValueError):
        avg_value = 0.0
    if avg_value > 0:
        return avg_value

    cum_quote = payload.get("cumQuote")
    executed_qty = payload.get("executedQty")
    try:
        cum_quote_value = float(cum_quote)
        executed_qty_value = float(executed_qty)
    except (TypeError, ValueError):
        return None
    if executed_qty_value <= 0:
        return None
    return cum_quote_value / executed_qty_value


def _quantize_to_step(value: float, step: float, mode: str = "nearest") -> float:
    if value <= 0 or step <= 0:
        return value
    value_dec = Decimal(str(value))
    step_dec = Decimal(str(step))
    units = value_dec / step_dec
    if mode == "down":
        rounding = ROUND_DOWN
    elif mode == "up":
        rounding = ROUND_UP
    else:
        rounding = ROUND_HALF_UP
    units = units.to_integral_value(rounding=rounding)
    return float(units * step_dec)


def _as_positive_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _safe_float(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _group_exchange_fills(rows: list[dict[str, Any]]) -> list[ExchangeFillSummary]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        order_id = _extract_order_id(row)
        if order_id is None:
            continue
        item = grouped.setdefault(
            order_id,
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "side": "BUY" if bool(row.get("buyer")) else "SELL",
                "quantity": 0.0,
                "quote_qty": 0.0,
                "realized_pnl": 0.0,
                "fill_time_ms": 0,
            },
        )
        item["quantity"] += _safe_float(row.get("qty"))
        item["quote_qty"] += _safe_float(row.get("quoteQty"))
        item["realized_pnl"] += _safe_float(row.get("realizedPnl"))
        item["fill_time_ms"] = max(
            item["fill_time_ms"], int(row.get("time") or 0))

    summaries: list[ExchangeFillSummary] = []
    for order_id, item in grouped.items():
        quantity = float(item["quantity"])
        average_price = float(item["quote_qty"]) / \
            quantity if quantity > 0 else 0.0
        fill_time_ms = int(item["fill_time_ms"])
        summaries.append(
            ExchangeFillSummary(
                symbol=str(item["symbol"]),
                order_id=order_id,
                side=str(item["side"]),
                quantity=quantity,
                average_price=average_price,
                realized_pnl=float(item["realized_pnl"]),
                fill_time_ms=fill_time_ms,
                fill_time=iso(_ms_to_utc(fill_time_ms)
                              ) if fill_time_ms > 0 else "",
            )
        )
    summaries.sort(key=lambda item: (item.fill_time_ms, item.order_id))
    return summaries


def _match_trade_to_fills(
    fills: list[ExchangeFillSummary],
    *,
    trade_side: str,
    quantity: float,
    opened_at_ms: int,
) -> tuple[ExchangeFillSummary | None, ExchangeFillSummary | None]:
    entry_side = "BUY" if trade_side.upper() == Side.LONG.value else "SELL"
    exit_side = "SELL" if entry_side == "BUY" else "BUY"
    qty_tolerance = max(quantity * 0.05, 0.00001)

    entry_fill: ExchangeFillSummary | None = None
    for fill in fills:
        if fill.side != entry_side:
            continue
        if abs(fill.quantity - quantity) > qty_tolerance:
            continue
        if fill.fill_time_ms + 60_000 < opened_at_ms:
            continue
        entry_fill = fill
        break

    exit_fill: ExchangeFillSummary | None = None
    if entry_fill is not None:
        for fill in fills:
            if fill.order_id == entry_fill.order_id:
                continue
            if fill.side != exit_side:
                continue
            if abs(fill.quantity - quantity) > qty_tolerance:
                continue
            if fill.fill_time_ms < entry_fill.fill_time_ms:
                continue
            exit_fill = fill
            break

    return entry_fill, exit_fill


def _iso_to_ms(raw: str) -> int:
    return int(_dt_from_iso(raw).timestamp() * 1000)


def _dt_from_iso(raw: str):
    from datetime import datetime

    return datetime.fromisoformat(raw)


def _ms_to_utc(value: int):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
