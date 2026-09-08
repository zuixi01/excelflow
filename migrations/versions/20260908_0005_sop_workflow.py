"""Add versioned SOP tasks, maintenance records and revision history."""

from alembic import op

revision = "20260908_0005"
down_revision = "20260903_0004"
branch_labels = None
depends_on = None


def upgrade():
    from excel_auditor.workflow.repository import metadata
    metadata.create_all(op.get_bind())


def downgrade():
    from excel_auditor.workflow.repository import metadata
    metadata.drop_all(op.get_bind())
