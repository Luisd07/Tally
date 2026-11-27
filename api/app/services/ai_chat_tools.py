"""The tools Claude may call from the money chat (#174).

Every tool reads only the signed-in user's rows, and none changes anything:
`propose_changes` (#223) stores a proposed action the user confirms or dismisses in the app.
Results carry dates, merchants or bank descriptions, amounts, categories, buckets, income
and subscription names, the Inbox grouped by merchant, and the tax summary: never account
names, numbers or balances. The one exception is `payoff_plan` (#243), which can't advise
without them: each card's name (digits masked), what it owes, its APR and payments, never
its account number.
Spending totals use the same rules as the dashboard (`app.services.dashboard.spending`), so
the chat's numbers match the app's.
"""

import json
import re
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import Select, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import NotFound, UnprocessableEntity
from app.models import Category, IncomeEntry, Rule, Subscription, Transaction
from app.models.enums import (
    AIActionSource,
    Bucket,
    Cadence,
    CategoryKind,
    MatchField,
    MatchType,
    SubscriptionKind,
)
from app.schemas.ai_actions import MAX_STEPS, MAX_TRANSACTIONS, AIActionOut
from app.schemas.tax import TaxSummaryQuery
from app.services import accounts, ai_actions, tax
from app.services.dashboard import spending
from app.services.inbox import inbox_base, needs_resplit_clause, suggestions
from app.services.insights import safe_name
from app.services.splits import has_split_children
from app.services.transactions import unmatched_manual_clause

MAX_ROWS = 50
# The whole Inbox fits one proposal, which may change at most this many transactions.
MAX_INBOX_ROWS = MAX_TRANSACTIONS
# Groups `inbox` lists, largest first, so a long tail of one-offs can't swamp the answer.
MAX_INBOX_GROUPS = 120
MAX_MONTHS = 36
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")

# Optional arguments are "" for "not set": plain strings are the shape strict tools document.
_MONTH_FIELD = {"type": "string", "description": 'YYYY-MM, or "" for no limit'}

# The steps of `app.schemas.ai_actions.ActionStep`, which checks them again in full. Patches
# set only some fields, which strict tools can't express (they require every property), so
# `propose_changes` isn't strict.
_ID = {"type": "string", "format": "uuid"}
_IDS = {"type": "array", "items": _ID, "minItems": 1, "maxItems": MAX_TRANSACTIONS}
_CLEARABLE_ID = {"type": ["string", "null"], "format": "uuid", "description": "null clears it"}
_DATE = {"type": "string", "format": "date"}
_BUCKET = {"type": "string", "enum": [b.value for b in Bucket]}
_NEGATIVE_AMOUNT = {"type": "string", "description": 'Negative, as "-11.99"'}


def _step(kind: str, description: str, **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {"kind": {"const": kind}, **properties},
        "required": ["kind", *properties],
        "additionalProperties": False,
    }


def _object(required: list[str], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_SUBSCRIPTION_FIELDS: dict[str, Any] = {
    "name": {"type": "string", "maxLength": 100},
    "amount": _NEGATIVE_AMOUNT,
    "cadence": {"type": "string", "enum": [c.value for c in Cadence]},
    "kind": {"type": "string", "enum": [k.value for k in SubscriptionKind]},
    "account_id": _CLEARABLE_ID,
    "category_id": _CLEARABLE_ID,
    "bucket": _BUCKET,
    "next_due": _DATE,
    "active": {"type": "boolean"},
    "start_date": _DATE,
    "end_date": _DATE,
    "business_pct": {"type": "integer", "minimum": 0, "maximum": 100},
    "match_text": {"type": "string", "maxLength": 200},
}

_TRANSACTION_PATCH = _object(
    [],
    category_id=_CLEARABLE_ID,
    bucket=_BUCKET,
    notes={"type": "string", "maxLength": 2000},
    reviewed={"type": "boolean"},
    merchant={"type": "string", "maxLength": 200},
)

_STEP_SCHEMAS = [
    _step(
        "categorize_transactions",
        "Apply one patch to every transaction in ids. Set only the fields to change.",
        ids=_IDS,
        patch=_TRANSACTION_PATCH,
    ),
    _step(
        "categorize_inbox_groups",
        "Apply one patch to every transaction in the named Inbox groups, by their group "
        "name from inbox, without listing ids. Set only the fields to change.",
        groups={"type": "array", "items": {"type": "string"}, "minItems": 1},
        patch=_TRANSACTION_PATCH,
    ),
    _step("mark_reviewed", "Mark the transactions in ids as reviewed.", ids=_IDS),
    _step(
        "create_category",
        "Create a category. Pick a fresh uuid for id so later steps can use it.",
        category=_object(
            ["id", "name", "kind"],
            id=_ID,
            name={"type": "string", "maxLength": 100},
            kind={"type": "string", "enum": [k.value for k in CategoryKind]},
            monthly_budget={"type": "string", "description": 'As "250.00"'},
        ),
    ),
    _step(
        "update_category",
        "Change a category: its budget, name, or archive it. Set only the fields to change.",
        id=_ID,
        patch=_object(
            [],
            name={"type": "string", "maxLength": 100},
            monthly_budget={
                "type": ["string", "null"],
                "description": 'Per month, as "250.00"; null clears it',
            },
            archived={"type": "boolean"},
        ),
    ),
    _step(
        "create_rule",
        "Create a rule for new bank rows. Pick a fresh uuid for id. Set at least one set_*.",
        rule=_object(
            ["id", "priority", "match_field", "match_type", "pattern"],
            id=_ID,
            priority={"type": "integer", "minimum": 0, "description": "Lower runs first"},
            match_field={"type": "string", "enum": [f.value for f in MatchField]},
            match_type={"type": "string", "enum": [t.value for t in MatchType]},
            pattern={"type": "string", "maxLength": 200},
            set_category_id=_ID,
            set_bucket=_BUCKET,
            set_merchant={"type": "string", "maxLength": 200},
        ),
    ),
    _step(
        "create_subscription",
        "Create a subscription or bill. Pick a fresh uuid for id.",
        subscription=_object(["id", "name", "amount", "cadence"], id=_ID, **_SUBSCRIPTION_FIELDS),
    ),
    _step(
        "update_subscription",
        "Change a subscription. Set only the fields to change.",
        id=_ID,
        patch=_object([], **_SUBSCRIPTION_FIELDS),
    ),
]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_transactions",
        "description": (
            "Find transactions. Returns the total count and sum of every match, and up to "
            f"{MAX_ROWS} of them newest first. Amounts are negative for money out. Filters "
            'are combined; pass "" for any you don\'t need.'
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": 'Matched against the merchant, description and notes, or "".',
                },
                "start_month": _MONTH_FIELD,
                "end_month": _MONTH_FIELD,
                "category": {
                    "type": "string",
                    "description": 'A category name as listed with the question, or "".',
                },
                "bucket": {"type": "string", "enum": [b.value for b in Bucket] + [""]},
            },
            "required": ["text", "start_month", "end_month", "category", "bucket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inbox",
        "description": (
            f"The user's Inbox (its newest {MAX_INBOX_ROWS} rows) grouped by merchant, "
            f"largest first, up to {MAX_INBOX_GROUPS} groups: each group's name, count, "
            "total, last date, one sample bank description, the category and bucket "
            "suggested from history or earlier AI suggestions, and mixed when those "
            "disagree. Also total_rows, shown_rows, shown_groups, truncated, and left_out: "
            "rows that need the user (pending, needs_resplit, unmatched_manual), which "
            "aren't in any group."
        ),
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "spending_totals",
        "description": (
            "Total spending (personal and business buckets, settled, as positive amounts with "
            "refunds netted) between two months inclusive, grouped by category, month or bucket."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "start_month": {"type": "string", "description": "YYYY-MM"},
                "end_month": {"type": "string", "description": "YYYY-MM"},
                "group_by": {"type": "string", "enum": ["category", "month", "bucket"]},
            },
            "required": ["start_month", "end_month", "group_by"],
            "additionalProperties": False,
        },
    },
    {
        "name": "income",
        "description": "Income entries (expected and received pay) between two months inclusive.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "start_month": {"type": "string", "description": "YYYY-MM"},
                "end_month": {"type": "string", "description": "YYYY-MM"},
            },
            "required": ["start_month", "end_month"],
            "additionalProperties": False,
        },
    },
    {
        "name": "subscriptions",
        "description": "The user's subscriptions and bills: name, amount, cadence, next due date.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "categories",
        "description": (
            "The user's categories: id, name, kind, whether archived, and monthly budget."
        ),
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rules",
        "description": (
            "The user's categorization rules: id, what each matches, and what it sets."
        ),
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "tax_summary",
        "description": "The year's Schedule C summary: deductible spend by tax line, and notes.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"year": {"type": "integer"}},
            "required": ["year"],
            "additionalProperties": False,
        },
    },
    {
        "name": "payoff_plan",
        "description": (
            "The credit card payoff plan: each card owing a balance (name, owed, APR, 0% promo "
            "end and the monthly payment that clears it by then, planned payment) paid on its "
            "own, the debt-free month and total interest at the planned payments, and the "
            "avalanche (highest APR in effect first, re-ranked as promos end) and snowball "
            "(smallest balance first) strategies for a monthly budget: order to pay, debt-free "
            "month, interest, and interest saved. When clamped is true, the budget asked for "
            "(requested_budget) was less than the monthly payments and was raised to cover them "
            "(budget): say so."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "monthly_budget": {
                    "type": "string",
                    "description": (
                        'Total to pay across the cards each month, as "600.00", or "" for the '
                        "sum of the planned payments"
                    ),
                },
            },
            "required": ["monthly_budget"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_changes",
        "description": (
            "Propose changes for the user to confirm with one tap in the app. Nothing changes "
            "until they do, and they can undo it. All steps apply together, in order. Use ids "
            "from the other tools, or Inbox group names from inbox; returns the stored "
            "proposal, or an error naming what's wrong."
        ),
        "strict": False,
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "maxLength": 300,
                    "description": "One plain sentence the user reads before confirming.",
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_STEPS,
                    "items": {"anyOf": _STEP_SCHEMAS},
                },
            },
            "required": ["summary", "steps"],
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    """A bad argument; the message goes back to Claude as an error result."""


def _month(value: str | None, field: str) -> tuple[int, int] | None:
    if not value:
        return None
    match = _MONTH.match(value)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ToolError(f"{field} must be YYYY-MM")
    return int(match.group(1)), int(match.group(2))


def _range(start: str | None, end: str | None) -> tuple[date | None, date | None]:
    """`[start, end)` dates for an inclusive month range; either side may be open."""
    first, last = _month(start, "start_month"), _month(end, "end_month")
    start_date = date(first[0], first[1], 1) if first else None
    end_date = None
    if last:
        end_date = date(last[0] + (last[1] == 12), last[1] % 12 + 1, 1)
    if start_date and end_date:
        if end_date <= start_date:
            raise ToolError("end_month is before start_month")
        if (end_date.year - start_date.year) * 12 + end_date.month - start_date.month > MAX_MONTHS:
            raise ToolError(f"a range can be at most {MAX_MONTHS} months")
    return start_date, end_date


def _required_range(start: str, end: str) -> tuple[date, date]:
    start_date, end_date = _range(start, end)
    assert start_date is not None and end_date is not None
    return start_date, end_date


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _category_names(db: Session, user_id: uuid.UUID) -> dict[uuid.UUID, str]:
    return {c.id: c.name for c in db.scalars(select(Category).where(Category.user_id == user_id))}


def search_transactions(db: Session, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    names = _category_names(db, user_id)
    stmt = select(Transaction).where(Transaction.user_id == user_id, ~has_split_children())
    start, end = _range(args.get("start_month"), args.get("end_month"))
    if start:
        stmt = stmt.where(Transaction.posted_date >= start)
    if end:
        stmt = stmt.where(Transaction.posted_date < end)
    if text := (args.get("text") or "").strip():
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            Transaction.description_raw.ilike(pattern, escape="\\")
            | Transaction.merchant.ilike(pattern, escape="\\")
            | Transaction.notes.ilike(pattern, escape="\\")
        )
    if category := args.get("category"):
        ids = [id for id, name in names.items() if name.casefold() == category.casefold()]
        if not ids:
            raise ToolError(f"no category named {category!r}; known: {sorted(names.values())}")
        stmt = stmt.where(Transaction.category_id.in_(ids))
    if bucket := args.get("bucket"):
        if bucket not in {b.value for b in Bucket}:
            raise ToolError("unknown bucket")
        stmt = stmt.where(Transaction.bucket == Bucket(bucket))

    matches = stmt.subquery()
    totals = db.execute(select(func.count(), func.coalesce(func.sum(matches.c.amount), 0))).one()
    rows = db.scalars(
        stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc()).limit(MAX_ROWS)
    )
    return {
        "match_count": totals[0],
        "match_sum": _money(Decimal(totals[1])),
        "shown": [
            {
                "id": str(row.id),
                "date": row.posted_date.isoformat(),
                "merchant": row.merchant or row.description_raw,
                "amount": _money(row.amount),
                "category": names.get(row.category_id) if row.category_id else None,
                "category_id": str(row.category_id) if row.category_id else None,
                "bucket": row.bucket.value,
                "pending": row.pending,
            }
            for row in rows
        ],
    }


_DIGITS = re.compile(r"\d{4,}")
_NOT_LETTERS = re.compile(r"[^A-Z&']+")


def _sanitize(description: str) -> str:
    """A bank description with its whitespace collapsed and runs of four or more digits
    masked. Only digits: a name or place in the description is returned as is."""
    return _DIGITS.sub("####", " ".join(description.split()))


def _description_key(description: str) -> str:
    """A bank description without its digits and punctuation, so "SHELL OIL 1234" and
    "SHELL OIL #5678" group together."""
    return " ".join(_NOT_LETTERS.sub(" ", description.upper()).split()) or _sanitize(description)


def _left_out(db: Session, base: Select[Transaction], now: datetime) -> dict[str, int]:
    """How many Inbox rows need the user rather than a category, each counted once: pending
    rows can still change, a split parent needs a resplit, and an unmatched manual row
    needs its bank row found or deleted."""
    flags = base.with_only_columns(
        Transaction.pending.label("pending"),
        needs_resplit_clause().label("needs_resplit"),
        unmatched_manual_clause(now).label("unmatched_manual"),
    ).subquery()
    counts = {"pending": 0, "needs_resplit": 0, "unmatched_manual": 0}
    stmt = select(
        flags.c.pending, flags.c.needs_resplit, flags.c.unmatched_manual, func.count()
    ).group_by(flags.c.pending, flags.c.needs_resplit, flags.c.unmatched_manual)
    for pending, needs_resplit, unmatched_manual, count in db.execute(stmt):
        if pending:
            counts["pending"] += count
        elif needs_resplit:
            counts["needs_resplit"] += count
        elif unmatched_manual:
            counts["unmatched_manual"] += count
    return counts


def _inbox_groups(
    db: Session, user_id: uuid.UUID, now: datetime
) -> tuple[list[Transaction], dict[str, list[Transaction]]]:
    """The newest Inbox rows a category can settle, and the same rows by group key: the
    merchant, or the bank description without its digits. `inbox` shows these groups and
    `categorize_inbox_groups` steps name them, so both must group the same way."""
    stmt = (
        inbox_base(user_id, now)
        .where(
            Transaction.pending.is_(False),
            ~needs_resplit_clause(),
            ~unmatched_manual_clause(now),
        )
        .order_by(Transaction.posted_date.desc(), Transaction.id.desc())
        .limit(MAX_INBOX_ROWS)
    )
    rows = list(db.scalars(stmt))
    groups: dict[str, list[Transaction]] = {}
    for row in rows:
        groups.setdefault(row.merchant or _description_key(row.description_raw), []).append(row)
    return rows, groups


def inbox(db: Session, user_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    base = inbox_base(user_id, now)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    left_out = _left_out(db, base, now)
    rows, groups = _inbox_groups(db, user_id, now)
    picks = suggestions(db, user_id, rows)

    shown = []
    for key, members in sorted(groups.items(), key=lambda item: -len(item[1])):
        votes = Counter(
            (pick[0], pick[1])
            for row in members
            if (pick := picks.get(row.id)) is not None and pick[0] is not None
        )
        category, bucket = votes.most_common(1)[0][0] if votes else (None, None)
        group: dict[str, Any] = {
            "group": key,
            "count": len(members),
            "total": _money(sum((row.amount for row in members), Decimal("0.00"))),
            "last_date": members[0].posted_date.isoformat(),
            "sample_description": _sanitize(members[0].description_raw),
            "suggested_category_id": str(category) if category else None,
            "bucket_hint": bucket.value if bucket else None,
        }
        if len(votes) > 1:
            group["mixed"] = True
        shown.append(group)

    shown = shown[:MAX_INBOX_GROUPS]
    settleable = total - sum(left_out.values())
    return {
        "total_rows": total,
        "shown_rows": sum(group["count"] for group in shown),
        "shown_groups": len(shown),
        "truncated": settleable > len(rows) or len(groups) > len(shown),
        "left_out": left_out,
        "groups": shown,
    }


def _expand_inbox_groups(
    db: Session, user_id: uuid.UUID, steps: list[Any], now: datetime
) -> list[Any]:
    """`steps` with each `categorize_inbox_groups` step replaced by the
    `categorize_transactions` step it stands for: the ids of the named groups' rows, found
    the way `inbox` found them."""
    groups: dict[str, list[Transaction]] | None = None
    expanded = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("kind") != "categorize_inbox_groups":
            expanded.append(step)
            continue
        keys = step.get("groups")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ToolError(f"steps.{index}: groups must be a list of group names from inbox")
        if groups is None:
            groups = _inbox_groups(db, user_id, now)[1]
        unknown = [key for key in keys if key not in groups]
        if unknown:
            raise ToolError(
                f"steps.{index}: no Inbox group named {', '.join(map(repr, unknown))}; "
                "use the group names inbox returned"
            )
        ids = [str(row.id) for key in dict.fromkeys(keys) for row in groups[key]]
        expanded.append({"kind": "categorize_transactions", "ids": ids, "patch": step.get("patch")})
    return expanded


def spending_totals(db: Session, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    start, end = _required_range(args["start_month"], args["end_month"])
    group_by = args["group_by"]
    month = func.to_char(Transaction.posted_date, "YYYY-MM")
    key = {
        "category": Transaction.category_id,
        "month": month,
        "bucket": Transaction.bucket,
    }.get(group_by)
    if key is None:
        raise ToolError("group_by must be category, month or bucket")
    stmt = spending(
        select(key, func.sum(-Transaction.amount)).where(Transaction.user_id == user_id),
        start,
        end,
    ).group_by(key)
    names = _category_names(db, user_id)
    groups = []
    for group, spent in db.execute(stmt):
        if group_by == "category":
            label = names.get(group, "Uncategorized") if group else "Uncategorized"
        elif group_by == "bucket":
            label = group.value
        else:
            label = group
        groups.append({"group": label, "spent": _money(spent)})
    groups.sort(key=lambda g: g["group"] if group_by == "month" else -Decimal(g["spent"]))
    total = sum((Decimal(g["spent"]) for g in groups), Decimal("0.00"))
    return {"total_spent": _money(total), "groups": groups}


def income(db: Session, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    start, end = _required_range(args["start_month"], args["end_month"])
    rows = db.scalars(
        select(IncomeEntry)
        .where(IncomeEntry.user_id == user_id, IncomeEntry.date >= start, IncomeEntry.date < end)
        .order_by(IncomeEntry.date)
        .limit(200)
    )
    entries = [
        {
            "date": row.date.isoformat(),
            "type": row.type.value,
            "description": row.description,
            "expected": _money(row.expected),
            "received": _money(row.received),
        }
        for row in rows
    ]
    received = sum((Decimal(e["received"]) for e in entries if e["received"]), Decimal("0.00"))
    return {"total_received": _money(received), "entries": entries}


def subscriptions(db: Session, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    rows = db.scalars(
        select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.name)
    )
    return {
        "subscriptions": [
            {
                "id": str(row.id),
                "name": row.name,
                "kind": row.kind.value,
                "amount": _money(row.amount),
                "cadence": row.cadence.value,
                "next_due": row.next_due.isoformat() if row.next_due else None,
                "active": row.active,
                "bucket": row.bucket.value,
                "category_id": str(row.category_id) if row.category_id else None,
            }
            for row in rows
        ]
    }


def categories(db: Session, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    rows = db.scalars(select(Category).where(Category.user_id == user_id).order_by(Category.name))
    return {
        "categories": [
            {
                "id": str(row.id),
                "name": row.name,
                "kind": row.kind.value,
                "archived": row.archived,
                "monthly_budget": _money(row.monthly_budget),
            }
            for row in rows
        ]
    }


def rules(db: Session, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    names = _category_names(db, user_id)
    rows = db.scalars(
        select(Rule)
        .where(Rule.user_id == user_id)
        .order_by(Rule.priority, Rule.created_at, Rule.id)
    )
    return {
        "rules": [
            {
                "id": str(row.id),
                "pattern": row.pattern,
                "match_field": row.match_field.value,
                "match_type": row.match_type.value,
                "set_category_id": str(row.set_category_id) if row.set_category_id else None,
                "set_category": names.get(row.set_category_id) if row.set_category_id else None,
                "set_bucket": row.set_bucket.value if row.set_bucket else None,
                "set_merchant": row.set_merchant,
                "active": row.active,
            }
            for row in rows
        ]
    }


def tax_summary(
    db: Session, user_id: uuid.UUID, args: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    year = args["year"]
    if not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ToolError("year must be a four-digit year")
    return tax.get_tax_summary(db, user_id, TaxSummaryQuery(year=year), settings).model_dump(
        mode="json"
    )


def payoff_plan(
    db: Session, user_id: uuid.UUID, args: dict[str, Any], now: datetime
) -> dict[str, Any]:
    raw = args["monthly_budget"]
    budget = None
    if raw:
        try:
            budget = Decimal(raw).quantize(Decimal("0.01"))
        except ArithmeticError as exc:
            raise ToolError('monthly_budget must be an amount like "600.00"') from exc
        if not budget.is_finite() or not 0 <= budget < 10**10:
            raise ToolError("monthly_budget must be zero or more")
    result = accounts.get_payoff_plan(db, user_id, budget, now).model_dump(mode="json")
    for card in [*result["cards"], *result["avalanche"]["order"], *result["snowball"]["order"]]:
        card["name"] = safe_name(card["name"])
    return result


def propose_changes(
    db: Session,
    user_id: uuid.UUID,
    args: dict[str, Any],
    conversation_id: uuid.UUID | None,
    now: datetime,
) -> dict[str, Any]:
    """Store the proposal; a savepoint drops it whole when it's refused or fails."""
    steps = _expand_inbox_groups(db, user_id, args["steps"], now)
    try:
        with db.begin_nested():
            action = ai_actions.propose_action(
                db, user_id, args["summary"], steps, AIActionSource.chat, conversation_id
            )
    except NotFound as exc:
        raise ToolError(exc.detail) from exc
    except SQLAlchemyError as exc:
        logger.warning("Saving a chat proposal failed: {}", type(exc).__name__)
        raise ToolError("the proposal couldn't be saved") from exc
    return {
        "action_id": str(action.id),
        "summary": action.summary,
        "effects": AIActionOut.model_validate(action).effects.model_dump(),
        "status": action.status.value,
    }


def _problems(exc: UnprocessableEntity) -> str:
    """A refused proposal's details as one message: "steps.1: unknown category id ..."."""
    return "; ".join(
        ".".join(str(part) for part in detail["loc"] if part != "body") + ": " + detail["msg"]
        for detail in exc.details or []
    )


def run(
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    name: str,
    args: dict[str, Any],
    conversation_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Run tool `name` with `args`; returns the JSON result and whether it's an error.
    `conversation_id` is the chat a proposal belongs to; `now` decides which manual rows
    count as unmatched in the Inbox and the payoff plan's first month."""
    now = now or datetime.now(UTC)
    try:
        match name:
            case "search_transactions":
                result = search_transactions(db, user_id, args)
            case "inbox":
                result = inbox(db, user_id, now)
            case "spending_totals":
                result = spending_totals(db, user_id, args)
            case "income":
                result = income(db, user_id, args)
            case "subscriptions":
                result = subscriptions(db, user_id, args)
            case "categories":
                result = categories(db, user_id, args)
            case "rules":
                result = rules(db, user_id, args)
            case "tax_summary":
                result = tax_summary(db, user_id, args, settings)
            case "payoff_plan":
                result = payoff_plan(db, user_id, args, now)
            case "propose_changes":
                result = propose_changes(db, user_id, args, conversation_id, now)
            case _:
                raise ToolError(f"unknown tool {name!r}")
    except UnprocessableEntity as exc:
        return json.dumps({"error": _problems(exc) or exc.detail}), True
    except (ToolError, KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)}), True
    return json.dumps(result, default=str), False
