"""chat prompt version

Revision ID: b7d41c9e2f05
Revises: 4aba5362414a
Create Date: 2026-10-27 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d41c9e2f05"
down_revision: str | Sequence[str] | None = "4aba5362414a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "chat_conversations", sa.Column("prompt_version", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "chat_conversations",
        sa.Column("actions_supported", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("chat_conversations", "actions_supported")
    op.drop_column("chat_conversations", "prompt_version")
