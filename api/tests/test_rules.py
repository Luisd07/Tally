"""#51: the auto-categorization engine (`app.services.rules.apply_rules`) and
`GET/POST/PATCH/DELETE /api/rules`.

Account/category rows are built directly in these tests (not through `/api/accounts` or
`/api/categories`, which #31 owns).
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Category, Transaction, User
from app.models.enums import Bucket, MatchType
from app.services import rules as service
from tests.factories import make_account, make_category, make_rule, make_transaction

RULES = "/api/rules"


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "priority": 10,
        "match_field": "description",
        "match_type": "contains",
        "pattern": "HERTZ",
        "set_merchant": "Hertz",
    }
    body.update(overrides)
    return body


# --- engine: apply_rules ------------------------------------------------------------------


def test_first_match_wins_by_priority(db_session: Session, user: User) -> None:
    account = make_account(user)
    category_a = make_category(user, name="A")
    category_b = make_category(user, name="B")
    _persist(db_session, account, category_a, category_b)
    low_priority = make_rule(
        user, priority=1, pattern="HERTZ", set_category_id=category_a.id, set_merchant=None
    )
    high_priority = make_rule(
        user, priority=5, pattern="HERTZ", set_category_id=category_b.id, set_merchant=None
    )
    txn = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    _persist(db_session, low_priority, high_priority, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.category_id == category_a.id


def test_case_and_whitespace_insensitive_match(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, pattern="hertz   rental", set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    _persist(db_session, rule, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant == "Hertz"


@pytest.mark.parametrize(
    ("match_type", "pattern", "description", "expect_match"),
    [
        (MatchType.contains, "RENTAL", "HERTZ RENTAL CAR", True),
        (MatchType.contains, "TAXI", "HERTZ RENTAL CAR", False),
        (MatchType.equals, "HERTZ RENTAL CAR", "HERTZ RENTAL CAR", True),
        (MatchType.equals, "HERTZ", "HERTZ RENTAL CAR", False),
        (MatchType.starts_with, "HERTZ", "HERTZ RENTAL CAR", True),
        (MatchType.starts_with, "RENTAL", "HERTZ RENTAL CAR", False),
    ],
)
def test_each_match_type(
    db_session: Session,
    user: User,
    match_type: MatchType,
    pattern: str,
    description: str,
    expect_match: bool,
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, match_type=match_type, pattern=pattern, set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw=description)
    _persist(db_session, rule, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert (txn.merchant == "Hertz") is expect_match


def test_inactive_rule_is_ignored(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, active=False, set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    _persist(db_session, rule, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant is None
    assert txn.bucket == Bucket.personal


def test_another_users_rule_is_never_applied(
    db_session: Session, user: User, other_user: User
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    theirs = make_rule(other_user, set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    _persist(db_session, theirs, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant is None


def test_unmatched_row_keeps_its_defaults(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, pattern="HERTZ", set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw="WHOLE FOODS MARKET")
    _persist(db_session, rule, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant is None
    assert txn.category_id is None
    assert txn.bucket == Bucket.personal
    assert txn.reviewed is False


def test_matched_row_is_marked_reviewed(db_session: Session, user: User) -> None:
    """Spec "Auto-categorization": "Anything no rule catches lands in the Inbox as
    unreviewed" implies the converse for a row a rule did catch (#51 MV-4).
    """
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, pattern="HERTZ", set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    _persist(db_session, rule, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant == "Hertz"
    assert txn.reviewed is True


def test_regex_metacharacters_are_treated_literally(db_session: Session, user: User) -> None:
    """A literal `.` in the pattern must not act as a regex wildcard: "A.C" is not a
    substring of "ABC", even though it would match as a regex.
    """
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, match_type=MatchType.contains, pattern="A.C", set_merchant="Matched")
    txn = make_transaction(user, account, description_raw="ABC TRAVEL")
    _persist(db_session, rule, txn)

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant is None


def test_apply_rules_sets_bucket_only_for_matching_rows(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rule = make_rule(user, pattern="HERTZ", set_bucket=Bucket.reimbursable, set_merchant=None)
    matching = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    other = make_transaction(user, account, description_raw="WHOLE FOODS MARKET")
    _persist(db_session, rule, matching, other)

    service.apply_rules(db_session, user.id, [matching.id, other.id])

    assert matching.bucket == Bucket.reimbursable
    assert other.bucket == Bucket.personal


def test_apply_rules_with_no_transaction_ids_is_a_no_op(db_session: Session, user: User) -> None:
    assert service.apply_rules(db_session, user.id, []) is None


def test_rule_with_every_set_field_nulled_by_a_category_delete_is_skipped(
    db_session: Session, user: User
) -> None:
    """#51 UB-2/MV-6: deleting a category `ON DELETE SET NULL`s a rule's only
    `set_*` field. The now-no-op rule must be skipped, not matched -- otherwise
    it would wrongly stop the next, still-meaningful rule from being tried.
    """
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    noop_rule = make_rule(
        user, priority=1, pattern="HERTZ", set_category_id=category.id, set_merchant=None
    )
    fallback_rule = make_rule(user, priority=5, pattern="HERTZ", set_merchant="Hertz")
    txn = make_transaction(user, account, description_raw="HERTZ RENTAL CAR")
    _persist(db_session, noop_rule, fallback_rule, txn)

    db_session.execute(delete(Category).where(Category.id == category.id))
    db_session.flush()
    db_session.refresh(noop_rule)
    assert noop_rule.set_category_id is None  # confirm the FK really nulled it

    service.apply_rules(db_session, user.id, [txn.id])

    assert txn.merchant == "Hertz"  # the fallback rule matched instead
    assert txn.category_id is None


# --- router: GET/POST/PATCH/DELETE /api/rules ---------------------------------------------


def test_list_returns_rules_ordered_by_priority(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    low = make_rule(user, priority=5, pattern="A")
    high = make_rule(user, priority=1, pattern="B")
    _persist(db_session, low, high)

    r = db_client.get(RULES, headers=auth_headers)

    assert r.status_code == 200
    assert [row["priority"] for row in r.json()] == [1, 5]


def test_list_excludes_another_users_rules(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_rule(other_user))

    r = db_client.get(RULES, headers=auth_headers)

    assert r.status_code == 200
    assert r.json() == []


def test_create_returns_201_with_the_created_rule(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(), headers=auth_headers)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["priority"] == 10
    assert body["match_field"] == "description"
    assert body["match_type"] == "contains"
    assert body["pattern"] == "HERTZ"
    assert body["set_merchant"] == "Hertz"
    assert body["active"] is True
    assert "user_id" not in body


def test_create_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule_id = str(uuid.uuid4())
    first = db_client.post(RULES, json=_body(id=rule_id), headers=auth_headers)
    second = db_client.post(
        RULES, json=_body(id=rule_id, pattern="DIFFERENT"), headers=auth_headers
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["pattern"] == "HERTZ"


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    theirs = make_rule(other_user)
    _persist(db_session, theirs)

    r = db_client.post(RULES, json=_body(id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 409


def test_create_with_another_users_category_id_returns_404(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    other_category = make_category(other_user)
    _persist(db_session, other_category)

    r = db_client.post(
        RULES,
        json=_body(set_merchant=None, set_category_id=str(other_category.id)),
        headers=auth_headers,
    )

    assert r.status_code == 404


def test_create_rejects_a_pattern_too_long(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(pattern="x" * 201), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_an_empty_pattern(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(pattern=""), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_a_whitespace_only_pattern(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    """#51 MV-1: normalized to "", which would match (contains/starts_with) every
    transaction.
    """
    r = db_client.post(RULES, json=_body(pattern="   "), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_a_priority_out_of_int32_range(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(priority=2**31), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_a_negative_priority(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(priority=-1), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_an_unknown_match_type(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(match_type="regex"), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_match_field_merchant(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    """#51 UB-1/MV-2: `merchant` was dropped from `MatchField` since sync never
    sets `merchant` before rules run, so a `merchant` rule could never match.
    """
    r = db_client.post(RULES, json=_body(match_field="merchant"), headers=auth_headers)
    assert r.status_code == 422


def test_create_with_no_set_field_is_422(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(set_merchant=None), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(RULES, json=_body(user_id=str(uuid.uuid4())), headers=auth_headers)
    assert r.status_code == 422


def test_patch_updates_only_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule = make_rule(user, priority=10, pattern="HERTZ")
    _persist(db_session, rule)

    r = db_client.patch(f"{RULES}/{rule.id}", json={"priority": 1}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body["priority"] == 1
    assert body["pattern"] == "HERTZ"


def test_patch_clearing_the_only_set_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule = make_rule(user, set_merchant="Hertz", set_category_id=None, set_bucket=None)
    _persist(db_session, rule)

    r = db_client.patch(f"{RULES}/{rule.id}", json={"set_merchant": None}, headers=auth_headers)

    assert r.status_code == 422


def test_patch_clearing_one_of_several_set_fields_is_allowed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule = make_rule(user, set_merchant="Hertz", set_bucket=Bucket.business, set_category_id=None)
    _persist(db_session, rule)

    r = db_client.patch(f"{RULES}/{rule.id}", json={"set_merchant": None}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["set_merchant"] is None
    assert r.json()["set_bucket"] == "business"


def test_patch_on_another_users_row_is_404(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    theirs = make_rule(other_user)
    _persist(db_session, theirs)

    r = db_client.patch(f"{RULES}/{theirs.id}", json={"priority": 1}, headers=auth_headers)

    assert r.status_code == 404


def test_patch_with_another_users_category_id_returns_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    """#51 UB-4/MV-5: the only thing stopping a PATCH from pointing a rule at
    another user's category; a regression here would be a cross-user data leak.
    """
    rule = make_rule(user)
    other_category = make_category(other_user)
    _persist(db_session, rule, other_category)

    r = db_client.patch(
        f"{RULES}/{rule.id}",
        json={"set_category_id": str(other_category.id)},
        headers=auth_headers,
    )

    assert r.status_code == 404
    db_session.refresh(rule)
    assert rule.set_category_id is None


def test_patch_rejects_a_whitespace_only_pattern(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule = make_rule(user)
    _persist(db_session, rule)

    r = db_client.patch(f"{RULES}/{rule.id}", json={"pattern": "   "}, headers=auth_headers)

    assert r.status_code == 422


def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule = make_rule(user)
    _persist(db_session, rule)

    r = db_client.patch(f"{RULES}/{rule.id}", json={"priority": None}, headers=auth_headers)

    assert r.status_code == 422


def test_delete_returns_204_and_removes_the_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rule = make_rule(user)
    _persist(db_session, rule)

    r = db_client.delete(f"{RULES}/{rule.id}", headers=auth_headers)

    assert r.status_code == 204
    assert r.content == b""
    assert db_client.get(RULES, headers=auth_headers).json() == []


def test_delete_on_another_users_row_is_404(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    theirs = make_rule(other_user)
    _persist(db_session, theirs)

    r = db_client.delete(f"{RULES}/{theirs.id}", headers=auth_headers)

    assert r.status_code == 404


def test_delete_on_missing_id_is_404(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.delete(f"{RULES}/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404


# --- values_explained_by_rule (shared with #50 and #61) -------------------------------------


def _explained(db_session: Session, user: User, txn: Transaction) -> bool:
    return service.values_explained_by_rule(txn, service.load_rule_index(db_session, user.id))


def test_values_explained_when_the_matching_rule_set_exactly_these_values(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    _persist(db_session, make_rule(user, set_merchant="Hertz", set_category_id=category.id))
    txn = make_transaction(user, account, merchant="Hertz", category_id=category.id, reviewed=True)
    _persist(db_session, txn)

    assert _explained(db_session, user, txn) is True


def test_values_not_explained_when_the_row_differs_from_the_rule(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    _persist(db_session, account, make_rule(user, set_merchant="Hertz"))
    txn = make_transaction(user, account, merchant="Avis", reviewed=True)
    _persist(db_session, txn)

    assert _explained(db_session, user, txn) is False


def test_values_not_explained_when_no_rule_matches(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account, make_rule(user, pattern="AVIS", set_merchant="Avis"))
    txn = make_transaction(user, account, merchant="Hertz", reviewed=True)
    _persist(db_session, txn)

    assert _explained(db_session, user, txn) is False


def test_values_not_explained_when_the_rule_was_edited_after_applying(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    rule = make_rule(user, set_merchant="Hertz")
    _persist(db_session, account, rule)
    txn = make_transaction(user, account, merchant="Hertz", reviewed=True)
    _persist(db_session, txn)
    assert _explained(db_session, user, txn) is True

    rule.set_merchant = "Hertz Rentals"
    db_session.flush()

    assert _explained(db_session, user, txn) is False


def test_values_not_explained_when_the_user_changed_a_bucket_the_rule_does_not_set(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    _persist(db_session, make_rule(user, set_category_id=category.id))
    txn = make_transaction(
        user,
        account,
        merchant="Hertz",
        category_id=category.id,
        bucket=Bucket.business,
        reviewed=True,
    )
    _persist(db_session, txn)

    assert _explained(db_session, user, txn) is False


def test_values_explained_when_a_category_only_rule_left_the_default_bucket(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    _persist(db_session, make_rule(user, set_category_id=category.id))
    txn = make_transaction(
        user,
        account,
        merchant="Hertz",
        category_id=category.id,
        bucket=Bucket.personal,
        reviewed=True,
    )
    _persist(db_session, txn)

    assert _explained(db_session, user, txn) is True


def test_values_not_explained_when_the_user_renamed_the_merchant_a_category_only_rule_left_unset(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    _persist(db_session, make_rule(user, set_merchant=None, set_category_id=category.id))
    txn = make_transaction(
        user, account, merchant="My Renamed Merchant", category_id=category.id, reviewed=True
    )
    _persist(db_session, txn)

    assert _explained(db_session, user, txn) is False
