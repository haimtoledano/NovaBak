"""add_cbt_incremental_fields

Revision ID: a1b2c3d4e5f6
Revises: 293deac14745
Create Date: 2026-07-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '293deac14745'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add CBT / incremental backup fields to vms and backup_logs."""
    with op.batch_alter_table('vms', schema=None) as batch_op:
        batch_op.add_column(sa.Column('backup_type', sa.String(), server_default='full', nullable=True))
        batch_op.add_column(sa.Column('full_backup_day', sa.Integer(), server_default='0', nullable=True))
        batch_op.add_column(sa.Column('last_change_id', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('last_full_backup_id', sa.Integer(), nullable=True))

    with op.batch_alter_table('backup_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('backup_size_bytes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('disk_total_bytes', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove CBT / incremental backup fields."""
    with op.batch_alter_table('backup_logs', schema=None) as batch_op:
        batch_op.drop_column('disk_total_bytes')
        batch_op.drop_column('backup_size_bytes')

    with op.batch_alter_table('vms', schema=None) as batch_op:
        batch_op.drop_column('last_full_backup_id')
        batch_op.drop_column('last_change_id')
        batch_op.drop_column('full_backup_day')
        batch_op.drop_column('backup_type')
