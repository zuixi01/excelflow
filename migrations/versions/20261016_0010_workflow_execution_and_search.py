"""Durable workflow outbox and indexed task summaries."""
from alembic import op
import sqlalchemy as sa

revision = "20261016_0010"
down_revision = "20261016_0009"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    op.add_column("workflow_objects", sa.Column("creator_id", sa.String(200)))
    op.add_column("workflow_objects", sa.Column("parent_task_id", sa.String(64)))
    op.add_column("workflow_objects", sa.Column("search_summary", sa.Text(), nullable=False, server_default=""))
    table = sa.table("workflow_objects", sa.column("id", sa.String), sa.column("payload", sa.JSON),
                     sa.column("creator_id", sa.String), sa.column("parent_task_id", sa.String), sa.column("search_summary", sa.Text))
    cursor = ""
    while True:
        batch = bind.execute(sa.select(table.c.id, table.c.payload).where(table.c.id > cursor).order_by(table.c.id).limit(500)).mappings().all()
        if not batch:
            break
        for row in batch:
            payload = row["payload"]
            bind.execute(table.update().where(table.c.id == row["id"]).values(creator_id=payload.get("actor"), parent_task_id=payload.get("task_id"),
                search_summary=(str(payload.get("name") or "") + str(payload.get("filename") or "")).casefold()))
        cursor = batch[-1]["id"]
    op.create_index("ix_workflow_objects_creator_page", "workflow_objects", ["tenant_id", "kind", "creator_id", "updated_at", "id"])
    op.create_index("ix_workflow_objects_status_page", "workflow_objects", ["tenant_id", "kind", "status", "updated_at", "id"])
    op.create_index("ix_workflow_objects_parent_task", "workflow_objects", ["tenant_id", "kind", "parent_task_id", "created_at", "id"])
    op.create_table("workflow_executions",
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(200), nullable=False), sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("worker", sa.String(200)),
        sa.Column("token", sa.String(64)), sa.Column("lease_until", sa.String(40)),
        sa.Column("available_at", sa.String(40), nullable=False), sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("error", sa.Text()), sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.String(40)), sa.Column("finished_at", sa.String(40)))
    op.create_index("ix_workflow_executions_recovery", "workflow_executions", ["status", "available_at", "lease_until"])
    op.create_index("ix_workflow_executions_object", "workflow_executions", ["tenant_id", "object_id", "revision"])


def downgrade():
    op.drop_table("workflow_executions")
    op.drop_index("ix_workflow_objects_creator_page", table_name="workflow_objects")
    op.drop_index("ix_workflow_objects_status_page", table_name="workflow_objects")
    op.drop_index("ix_workflow_objects_parent_task", table_name="workflow_objects")
    op.drop_column("workflow_objects", "search_summary")
    op.drop_column("workflow_objects", "creator_id")
    op.drop_column("workflow_objects", "parent_task_id")
