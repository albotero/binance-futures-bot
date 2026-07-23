from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Position


class SQLiteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    realized_pnl REAL DEFAULT 0,
                    strategy TEXT,
                    stop_loss_price REAL,
                    take_profit_price REAL,
                    trailing_stop_price REAL,
                    status TEXT NOT NULL,
                    close_reason TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    metadata TEXT
                )
                """
            )
            self._ensure_trade_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def _ensure_trade_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(trades)").fetchall()
        columns = {row[1] for row in rows}
        for name, column_type in (
            ("stop_loss_price", "REAL"),
            ("take_profit_price", "REAL"),
            ("trailing_stop_price", "REAL"),
        ):
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {name} {column_type}")

    def record_open(self, position: Position, metadata: dict[str, Any] | None = None) -> int:
        payload = dict(metadata or {})
        if position.entry_order_id is not None:
            payload["entry_order_id"] = position.entry_order_id
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    symbol, side, quantity, entry_price, strategy,
                    stop_loss_price, take_profit_price, trailing_stop_price,
                    status, opened_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.symbol,
                    position.side.value,
                    position.quantity,
                    position.entry_price,
                    position.strategy,
                    position.stop_loss_price,
                    position.take_profit_price,
                    position.trailing_stop_price,
                    position.status.value,
                    position.opened_at,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def record_close(self, position: Position) -> None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, metadata FROM trades
                WHERE symbol = ? AND closed_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (position.symbol,),
            ).fetchone()
            metadata = self._parse_metadata(row["metadata"] if row else None)
            if position.entry_order_id is not None:
                metadata["entry_order_id"] = position.entry_order_id
            if position.exit_order_id is not None:
                metadata["exit_order_id"] = position.exit_order_id
            conn.execute(
                """
                UPDATE trades
                SET exit_price = ?, realized_pnl = ?, status = ?, close_reason = ?, closed_at = ?, metadata = ?
                WHERE id = ?
                """,
                (
                    position.current_price,
                    position.realized_pnl,
                    position.status.value,
                    position.close_reason,
                    position.updated_at,
                    json.dumps(metadata, sort_keys=True),
                    int(row["id"]) if row else -1,
                ),
            )

    def list_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_open_trades(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_trades(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY symbol ASC, opened_at ASC, id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def close_trade_by_id(
        self,
        trade_id: int,
        *,
        exit_price: float,
        realized_pnl: float,
        status: str,
        close_reason: str,
        closed_at: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE trades
                SET exit_price = ?, realized_pnl = ?, status = ?, close_reason = ?, closed_at = ?
                WHERE id = ? AND closed_at IS NULL
                """,
                (
                    exit_price,
                    realized_pnl,
                    status,
                    close_reason,
                    closed_at,
                    trade_id,
                ),
            )

    def update_trade_from_exchange(
        self,
        trade_id: int,
        *,
        entry_price: float | None = None,
        exit_price: float | None = None,
        realized_pnl: float | None = None,
        opened_at: str | None = None,
        closed_at: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            if not row:
                return
            metadata = self._parse_metadata(row["metadata"])
            metadata.update(metadata_updates or {})
            conn.execute(
                """
                UPDATE trades
                SET entry_price = ?, exit_price = ?, realized_pnl = ?, opened_at = ?, closed_at = ?, metadata = ?
                WHERE id = ?
                """,
                (
                    row["entry_price"] if entry_price is None else entry_price,
                    row["exit_price"] if exit_price is None else exit_price,
                    row["realized_pnl"] if realized_pnl is None else realized_pnl,
                    row["opened_at"] if opened_at is None else opened_at,
                    row["closed_at"] if closed_at is None else closed_at,
                    json.dumps(metadata, sort_keys=True),
                    trade_id,
                ),
            )

    def total_realized_pnl(self) -> float:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0.0) AS total FROM trades WHERE closed_at IS NOT NULL"
            ).fetchone()
        if not row:
            return 0.0
        return float(row["total"] or 0.0)

    def list_snapshots(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT created_at, payload FROM snapshots ORDER BY id DESC LIMIT ?", (
                    limit,)
            ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = json.loads(row["payload"])
            if "created_at" not in payload:
                payload["created_at"] = row["created_at"]
            snapshots.append(payload)
        return snapshots

    def store_snapshot(self, payload: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO snapshots (created_at, payload) VALUES (?, ?)",
                (payload.get("created_at"), json.dumps(payload, sort_keys=True)),
            )

    @staticmethod
    def _parse_metadata(raw: Any) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
