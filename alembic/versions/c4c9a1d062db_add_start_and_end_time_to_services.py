"""add_start_and_end_time_to_services

Revision ID: c4c9a1d062db
Revises: 269044107d58
Create Date: 2026-10-01 16:32:00.055231

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4c9a1d062db'
down_revision: Union[str, Sequence[str], None] = '269044107d58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add start_time with temporary server_default to seamlessly handle existing rows
    op.add_column(
        'services',
        sa.Column(
            'start_time',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now() + interval '1 day'"),
            nullable=False
        )
    )

    # 2. Add end_time with temporary server_default
    op.add_column(
        'services',
        sa.Column(
            'end_time',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now() + interval '1 day 4 hours'"),
            nullable=False
        )
    )

    # 3. Create index on start_time for efficient range queries (e.g. start_time >= now())
    op.create_index(op.f('ix_services_start_time'), 'services', ['start_time'], unique=False)

    # 4. Add CheckConstraint ensuring end_time > start_time
    op.create_check_constraint(
        'services_end_time_after_start_time',
        'services',
        'end_time > start_time'
    )

    # 5. Drop server defaults so subsequent application inserts must explicitly provide start_time and end_time
    op.alter_column('services', 'start_time', server_default=None)
    op.alter_column('services', 'end_time', server_default=None)


def downgrade() -> None:
    op.drop_constraint('services_end_time_after_start_time', 'services', type_='check')
    op.drop_index(op.f('ix_services_start_time'), table_name='services')
    op.drop_column('services', 'end_time')
    op.drop_column('services', 'start_time')

