"""add_user_role_enum_and_inventory_unique_constraint

Revision ID: 269044107d58
Revises: 6821cd001b18
Create Date: 2026-09-16 19:27:37.458788

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '269044107d58'
down_revision: Union[str, Sequence[str], None] = '6821cd001b18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Unique constraint on inventory_items
    op.create_unique_constraint('uq_inventory_items_service_id_identifier_code', 'inventory_items', ['service_id', 'identifier_code'])

    # 2. Create user_role_enum type in PostgreSQL
    user_role_enum = sa.Enum('CUSTOMER', 'SELLER', 'ADMIN', name='user_role_enum')
    user_role_enum.create(op.get_bind(), checkfirst=True)

    # 3. Alter column with explicit USING clause to convert existing text to enum
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE user_role_enum USING UPPER(role)::user_role_enum")
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'CUSTOMER'::user_role_enum")


def downgrade() -> None:
    """Downgrade schema."""
    # 1. Alter column back to VARCHAR
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(20) USING LOWER(role::text)")
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'customer'")

    # 2. Drop user_role_enum
    user_role_enum = sa.Enum('CUSTOMER', 'SELLER', 'ADMIN', name='user_role_enum')
    user_role_enum.drop(op.get_bind(), checkfirst=True)

    # 3. Drop unique constraint
    op.drop_constraint('uq_inventory_items_service_id_identifier_code', 'inventory_items', type_='unique')

