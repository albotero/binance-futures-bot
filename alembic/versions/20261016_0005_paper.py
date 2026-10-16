"""Transactional paper run checkpoints and repeatable position history."""

import sqlalchemy as sa

from alembic import op

revision = "20261016_0005"
down_revision = "20261016_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("positions") as batch:
        batch.drop_constraint("uq_position_mode_symbol_side", type_="unique")
    op.create_index(
        "uq_position_open_mode_symbol_side",
        "positions",
        ["mode", "symbol", "side"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "paper_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_slot", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running','paused','stopping','stopped','faulted')",
            name="ck_paper_run_status",
        ),
        sa.UniqueConstraint("active_slot", name="uq_paper_active_run"),
        sa.CheckConstraint(
            "(status = 'stopped' AND active_slot IS NULL) OR "
            "(status != 'stopped' AND active_slot IS NOT NULL AND active_slot = 1)",
            name="ck_paper_active_slot",
        ),
    )
    op.create_table(
        "paper_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("bar_open", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", "bar_open", name="uq_paper_event_run_bar"),
    )
    op.create_index("ix_paper_events_run_id", "paper_events", ["run_id"])


def downgrade() -> None:
    # Refuse loss of restart state/history and an incompatible position collapse.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM paper_runs")):
        raise RuntimeError("export and remove paper runs before downgrading")
    duplicates = connection.scalar(
        sa.text(
            "SELECT count(*) FROM (SELECT mode,symbol,side FROM positions "
            "GROUP BY mode,symbol,side HAVING count(*) > 1)"
        )
    )
    if duplicates:
        raise RuntimeError("position history is incompatible with the old unique constraint")
    op.drop_index("ix_paper_events_run_id", table_name="paper_events")
    op.drop_table("paper_events")
    op.drop_table("paper_runs")
    op.drop_index("uq_position_open_mode_symbol_side", table_name="positions")
    with op.batch_alter_table("positions") as batch:
        batch.create_unique_constraint("uq_position_mode_symbol_side", ["mode", "symbol", "side"])
