"""sync timestamps and dupe info

Revision ID: 188d830287df
Revises: 2cd374f28ad2
Create Date: 2026-08-03 05:32:38.313349

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '188d830287df'
down_revision: str | None = '2cd374f28ad2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('instances', sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('instances', sa.Column('last_backfilled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('verdicts', sa.Column('dupe_info', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('verdicts', 'dupe_info')
    op.drop_column('instances', 'last_backfilled_at')
    op.drop_column('instances', 'last_polled_at')
