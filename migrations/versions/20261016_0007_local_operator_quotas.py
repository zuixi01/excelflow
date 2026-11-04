"""Add local operator quota records and normalize the workspace bootstrap."""

from alembic import op
import sqlalchemy as sa


revision = "20261016_0007"
down_revision = "20261016_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_auth_local_active_owner",
        "auth_users",
        ["tenant_id", "role"],
        unique=True,
        sqlite_where=sa.text("tenant_id = 'local' AND role = 'owner' AND status = 'active'"),
        postgresql_where=sa.text("tenant_id = 'local' AND role = 'owner' AND status = 'active'"),
    )
    op.create_table(
        "auth_user_quotas",
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("auth_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("max_running", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("max_queued", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_user_quotas_running", "auth_user_quotas", ["max_running"])
    op.execute(
        "INSERT INTO auth_user_quotas (user_id, max_running, max_queued, daily_limit, updated_at) "
        "SELECT id, 2, 10, 100, CURRENT_TIMESTAMP FROM auth_users "
        "WHERE tenant_id = 'local' AND role IN ('operator', 'viewer')"
    )


def downgrade() -> None:
    op.drop_index("ix_auth_user_quotas_running", table_name="auth_user_quotas")
    op.drop_table("auth_user_quotas")
    op.drop_index("uq_auth_local_active_owner", table_name="auth_users")
