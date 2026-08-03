"""verdict apply_at

Revision ID: 735bb431e62b
Revises: 2195e88c99a7
Create Date: 2026-08-03 23:28:41.694788

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "735bb431e62b"
down_revision: str | None = "2195e88c99a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("verdicts", sa.Column("apply_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("verdicts", "apply_at")
