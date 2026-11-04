"""Persist expiring login failure windows across API processes."""

from alembic import op
import sqlalchemy as sa

revision = "20261016_0009"
down_revision = "20261016_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("auth_rate_limits", sa.Column("key", sa.String(100), primary_key=True),
                    sa.Column("failures", sa.Integer(), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_auth_rate_limits_expires_at", "auth_rate_limits", ["expires_at"])


def downgrade():
    op.drop_table("auth_rate_limits")
