"""The split-exclusion rule shared by every service that totals transactions.

A transaction with its own children is a pre-split parent (`parent_id`; #80 adds
real splits through the API). Excluding it keeps a future split from being counted
alongside its children, which do count as ordinary rows. `#69` (dashboard) and
`#113` (receipts) both apply this; a real split (`#80`) should keep applying it too.

Constraint for #48 (the nightly sync): the parent keeps its bank `external_id`
(#80), so the `(account_id, external_id)` upsert will find it again. The bank is
the source of truth for `amount`, so a re-sync updates it on a split parent same
as any other row; the children (their sum no longer matching) are left alone,
and `reviewed` is reset to `False` only the sync where the amount actually
changed, so the user re-splits against the new amount and a later identical
sync doesn't re-flag an already-reviewed row.
"""

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import aliased

from app.models import Transaction


def has_split_children() -> ColumnElement[bool]:
    """`EXISTS`, true when some other transaction points at this one via
    `parent_id`. Scoped to the same `user_id` as the row it's compared against,
    since the DB doesn't enforce same-user `parent_id`.

    Builds a fresh alias on every call, so it's safe to use more than once in the
    same query.
    """
    child = aliased(Transaction)
    return (
        select(child.id)
        .where(child.parent_id == Transaction.id, child.user_id == Transaction.user_id)
        .exists()
    )
