"""Isolate account snapshots by quote denomination without relabeling old data."""

import sqlalchemy as sa

from alembic import op

revision = "20261016_0004"
down_revision = "20261015_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_snapshots",
        sa.Column("quote_asset", sa.String(16), nullable=False, server_default="UNKNOWN"),
    )
    op.create_index(
        "ix_account_snapshots_environment_quote_time",
        "account_snapshots",
        ["environment", "quote_asset", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_snapshots_environment_quote_time", table_name="account_snapshots")
    with op.batch_alter_table("account_snapshots") as batch:
        batch.drop_column("quote_asset")
