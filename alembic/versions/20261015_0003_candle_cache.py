"""add normalized historical candle cache

Revision ID: 20261015_0003
Revises: ad9c0130f3a8
Create Date: 2026-10-15 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

import futures_bot.persistence.models
from alembic import op

revision: str = "20261015_0003"
down_revision: str | None = "ad9c0130f3a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column(
            "open_time", futures_bot.persistence.models.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "close_time", futures_bot.persistence.models.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column("open", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("high", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("low", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("close", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("volume", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("quote_volume", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("taker_buy_volume", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("taker_buy_quote_volume", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "cached_at", futures_bot.persistence.models.UTCDateTime(timezone=True), nullable=False
        ),
        sa.CheckConstraint("trade_count >= 0", name="ck_candles_trade_count_non_negative"),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_candles_price_positive",
        ),
        sa.CheckConstraint(
            "high >= open AND high >= close AND high >= low",
            name="ck_candles_high_valid",
        ),
        sa.CheckConstraint(
            "low <= open AND low <= close AND low <= high",
            name="ck_candles_low_valid",
        ),
        sa.CheckConstraint(
            "volume >= 0 AND quote_volume >= 0 AND "
            "taker_buy_volume >= 0 AND taker_buy_quote_volume >= 0",
            name="ck_candles_volume_non_negative",
        ),
        sa.CheckConstraint("close_time > open_time", name="ck_candles_close_after_open"),
        sa.PrimaryKeyConstraint("symbol", "interval", "open_time"),
    )
    op.create_index(
        "ix_candles_interval_symbol_open_time",
        "candles",
        ["interval", "symbol", "open_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_candles_interval_symbol_open_time", table_name="candles")
    op.drop_table("candles")
