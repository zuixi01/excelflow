"""Add queue, retry, lease, and idempotency metadata to comparison jobs."""

from alembic import op
import sqlalchemy as sa


revision = "20261016_0008"
down_revision = "20261016_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comparison_jobs", sa.Column("queue_name", sa.String(length=32), nullable=False, server_default="normal"))
    op.add_column("comparison_jobs", sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("comparison_jobs", sa.Column("worker_id", sa.String(length=200), nullable=True))
    op.add_column("comparison_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("comparison_jobs", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.create_index("ix_comparison_jobs_idempotency_key", "comparison_jobs", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index("ix_comparison_jobs_idempotency_key", table_name="comparison_jobs")
    op.drop_column("comparison_jobs", "idempotency_key")
    op.drop_column("comparison_jobs", "lease_expires_at")
    op.drop_column("comparison_jobs", "worker_id")
    op.drop_column("comparison_jobs", "attempt")
    op.drop_column("comparison_jobs", "queue_name")
