"""Shared workflow admission and expiring resource leases."""
from alembic import op
import sqlalchemy as sa

revision = '20261103_0011'
down_revision = '20261016_0010'
branch_labels = depends_on = None


def upgrade():
    op.create_table('workflow_capacity_locks', sa.Column('name', sa.String(64), primary_key=True), sa.Column('version', sa.Integer(), nullable=False))
    op.create_table('workflow_resource_leases', sa.Column('token', sa.String(64), primary_key=True),
                    sa.Column('resource', sa.String(32), nullable=False), sa.Column('expires_at', sa.String(40), nullable=False))
    op.create_index('ix_workflow_resource_expiry', 'workflow_resource_leases', ['resource', 'expires_at'])


def downgrade():
    op.drop_table('workflow_resource_leases')
    op.drop_table('workflow_capacity_locks')
