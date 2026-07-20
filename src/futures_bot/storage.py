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
                    status TEXT NOT NULL,
                    close_reason TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def record_open(self, position: Position, metadata: dict[str, Any] | None = None) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    symbol, side, quantity, entry_price, strategy, status, opened_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.symbol,
                    position.side.value,
                    position.quantity,
                    position.entry_price,
                    position.strategy,
                    position.status.value,
                    position.opened_at,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def record_close(self, position: Position) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE trades
                SET exit_price = ?, realized_pnl = ?, status = ?, close_reason = ?, closed_at = ?
                WHERE id = (
                    SELECT id FROM trades
                    WHERE symbol = ? AND closed_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                )
                """,
                (
                    position.current_price,
                    position.realized_pnl,
                    position.status.value,
                    position.close_reason,
                    position.updated_at,
                    position.symbol,
                ),
            )

    def list_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

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
