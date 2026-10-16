"""Opaque owner authentication sessions and persistent login throttles."""

import sqlalchemy as sa

from alembic import op

revision = "20261016_0006"
down_revision = "20261016_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_owners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "web_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("web_owners.id"), nullable=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])
    op.create_table(
        "web_login_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_login_throttles_reset_at", "web_login_throttles", ["reset_at"])


def downgrade() -> None:
    op.drop_index("ix_web_login_throttles_reset_at", table_name="web_login_throttles")
    op.drop_table("web_login_throttles")
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_table("web_owners")
