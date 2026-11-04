"""Add versioned SOP tasks, maintenance records and revision history."""

from alembic import op
import sqlalchemy as sa

revision = "20260908_0005"
down_revision = "20260903_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("workflow_objects",
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("updated_at", sa.String(40), nullable=False))
    op.create_index("ix_workflow_objects_tenant_kind", "workflow_objects", ["tenant_id", "kind", "updated_at"])
    op.create_table("workflow_records",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("workflow_objects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("values_json", sa.JSON(), nullable=False), sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False), sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False), sa.Column("search_text", sa.Text(), nullable=False))
    op.create_index("ix_workflow_records_task_ordinal", "workflow_records", ["task_id", "ordinal"])
    op.create_table("workflow_revisions",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("workflow_objects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True), sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("detail", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("workflow_revisions")
    op.drop_table("workflow_records")
    op.drop_table("workflow_objects")
