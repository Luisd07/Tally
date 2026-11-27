"""add subscription business_pct and match_text

Revision ID: 32dd1d6d0d32
Revises: e29be82a938b
Create Date: 2026-10-16 23:53:59.889958

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "32dd1d6d0d32"
down_revision: str | Sequence[str] | None = "e29be82a938b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("subscriptions", sa.Column("business_pct", sa.SmallInteger(), nullable=True))
    op.add_column("subscriptions", sa.Column("match_text", sa.String(length=200), nullable=True))
    op.create_check_constraint(
        "business_pct_range",
        "subscriptions",
        "business_pct IS NULL OR (business_pct >= 0 AND business_pct <= 100)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("business_pct_range", "subscriptions", type_="check")
    op.drop_column("subscriptions", "match_text")
    op.drop_column("subscriptions", "business_pct")
