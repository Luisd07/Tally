"""seed irs mileage rates for existing users

Revision ID: af43478824fa
Revises: ee8d5e88ce6b
Create Date: 2026-10-19 21:13:57.803237

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "af43478824fa"
down_revision: str | Sequence[str] | None = "ee8d5e88ce6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# IRS standard mileage rates (#167, D5). Users created after this migration start with
# no rates and add them in the app.
_RATES = "(DATE '2025-01-01', 0.7000), (DATE '2026-01-01', 0.7250), (DATE '2026-07-01', 0.7600)"

# Every user gets every rate; a user who already has a row for that date keeps it.
SEED_SQL = f"""
INSERT INTO mileage_rates (id, user_id, effective_from, rate)
SELECT gen_random_uuid(), users.id, rates.effective_from, rates.rate
FROM users CROSS JOIN (VALUES {_RATES}) AS rates (effective_from, rate)
ON CONFLICT (user_id, effective_from) DO NOTHING
"""


def upgrade() -> None:
    """Seed the D5 rates for every existing user."""
    op.execute(SEED_SQL)


def downgrade() -> None:
    """Delete every row that matches a seeded (effective_from, rate) pair, whoever
    added it: a rate the user entered with the same date and value goes too.
    """
    op.execute(f"DELETE FROM mileage_rates WHERE (effective_from, rate) IN (VALUES {_RATES})")
