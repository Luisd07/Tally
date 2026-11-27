from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import Transaction, User
from app.models.enums import Bucket, Source
from tests.factories import make_account, make_category, make_transaction


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _inbox(db_client: TestClient, headers: dict[str, str], **params: str) -> dict:
    response = db_client.get("/api/inbox", params=params, headers=headers)
    assert response.status_code == 200
    return response.json()


def test_inbox_returns_exactly_the_users_unreviewed_rows(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    _persist(db_session, account, other_account)
    unreviewed = make_transaction(user, account, reviewed=False)
    pending = make_transaction(user, account, reviewed=False, pending=True)
    reviewed = make_transaction(user, account, reviewed=True)
    parent = make_transaction(user, account, reviewed=True)
    _persist(db_session, unreviewed, pending, reviewed, parent)
    child = make_transaction(user, account, reviewed=False, parent_id=parent.id)
    theirs = make_transaction(other_user, other_account, reviewed=False)
    _persist(db_session, child, theirs)

    page = _inbox(db_client, auth_headers)

    assert {item["id"] for item in page["items"]} == {
        str(unreviewed.id),
        str(pending.id),
        str(child.id),
    }
    assert page["total"] == 3


def test_inbox_total_is_independent_of_paging_and_every_row_appears_once(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rows = [
        make_transaction(user, account, reviewed=False, posted_date=date(2026, 10, 1 + i % 2))
        for i in range(5)
    ]
    _persist(db_session, *rows)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        page = _inbox(db_client, auth_headers, **params)
        assert page["total"] == 5
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(str(row.id) for row in rows)
    assert len(seen) == len(set(seen))


def test_inbox_orders_by_posted_date_then_id_descending(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rows = [
        make_transaction(user, account, reviewed=False, posted_date=date(2026, 10, d))
        for d in (1, 3, 3, 2)
    ]
    _persist(db_session, *rows)

    items = _inbox(db_client, auth_headers)["items"]

    keys = [(item["posted_date"], item["id"]) for item in items]
    assert keys == sorted(keys, reverse=True)


def test_inbox_rejects_an_invalid_cursor(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get("/api/inbox", params={"cursor": "not-base64!!"}, headers=auth_headers)

    assert response.status_code == 422


def test_inbox_suggests_category_and_bucket_from_the_latest_reviewed_row_for_the_merchant(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    old_category = make_category(user, name="Old")
    new_category = make_category(user, name="New")
    _persist(db_session, account, old_category, new_category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            merchant="Hertz",
            reviewed=True,
            category_id=old_category.id,
            bucket=Bucket.personal,
            posted_date=date(2026, 9, 1),
        ),
        make_transaction(
            user,
            account,
            merchant="Hertz",
            reviewed=True,
            category_id=new_category.id,
            bucket=Bucket.business,
            posted_date=date(2026, 9, 20),
        ),
    )
    item = make_transaction(user, account, merchant="Hertz", reviewed=False)
    _persist(db_session, item)

    [out] = _inbox(db_client, auth_headers)["items"]

    assert out["suggested_category_id"] == str(new_category.id)
    assert out["suggested_bucket"] == "business"


def test_inbox_suggestion_is_null_without_history_or_merchant_and_ignores_unreviewed_history(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    miss = make_transaction(user, account, merchant="Nobody", reviewed=False)
    no_merchant = make_transaction(user, account, merchant=None, reviewed=False)
    unreviewed_peer = make_transaction(
        user, account, merchant="Peer", reviewed=False, category_id=category.id
    )
    peer = make_transaction(user, account, merchant="Peer", reviewed=False)
    no_merchant_history = make_transaction(
        user, account, merchant=None, reviewed=True, category_id=category.id
    )
    _persist(db_session, miss, no_merchant, unreviewed_peer, peer, no_merchant_history)

    items = _inbox(db_client, auth_headers)["items"]

    assert len(items) == 4
    assert all(item["suggested_category_id"] is None for item in items)
    assert all(item["suggested_bucket"] is None for item in items)


def test_inbox_suggestions_do_not_leak_another_users_history(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    other_category = make_category(other_user)
    _persist(db_session, account, other_account, other_category)
    _persist(
        db_session,
        make_transaction(
            other_user,
            other_account,
            merchant="Hertz",
            reviewed=True,
            category_id=other_category.id,
            bucket=Bucket.business,
        ),
        make_transaction(user, account, merchant="Hertz", reviewed=False),
    )

    [out] = _inbox(db_client, auth_headers)["items"]

    assert out["suggested_category_id"] is None
    assert out["suggested_bucket"] is None


def test_inbox_suggestions_use_a_bounded_number_of_queries(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)

    def count_for(merchants: int) -> int:
        rows = [
            make_transaction(user, account, merchant=f"M{i}-{merchants}", reviewed=False)
            for i in range(merchants)
        ]
        _persist(db_session, *rows)
        statements: list[str] = []

        def record(conn, cursor, statement, *args):  # noqa: ANN001, ANN002, ANN202
            statements.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            _inbox(db_client, auth_headers)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    few = count_for(2)
    many = count_for(8)

    assert many == few


def test_reviewing_every_inbox_row_empties_the_inbox(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    _persist(
        db_session,
        *[make_transaction(user, account, reviewed=False, amount=Decimal(-i)) for i in range(1, 4)],
    )

    for item in _inbox(db_client, auth_headers)["items"]:
        response = db_client.patch(
            f"/api/transactions/{item['id']}", json={"reviewed": True}, headers=auth_headers
        )
        assert response.status_code == 200

    page = _inbox(db_client, auth_headers)
    assert page["total"] == 0
    assert page["items"] == []
    assert db_session.query(Transaction).filter_by(reviewed=False).count() == 0


NOW = datetime.now(UTC)


def test_inbox_flags_a_split_parent_sync_marked_for_resplit(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    plain = make_transaction(user, account, reviewed=False)
    resplit = make_transaction(user, account, reviewed=False)
    done = make_transaction(user, account, reviewed=True)
    _persist(db_session, plain, resplit, done)
    _persist(
        db_session,
        make_transaction(user, account, reviewed=True, parent_id=resplit.id),
        make_transaction(user, account, reviewed=True, parent_id=done.id),
    )

    page = _inbox(db_client, auth_headers)

    flags = {item["id"]: item["needs_resplit"] for item in page["items"]}
    assert flags == {str(plain.id): False, str(resplit.id): True}
    assert page["total"] == 2


def _manual(user: User, account, days_old: int, **kwargs: object) -> Transaction:  # noqa: ANN001
    return make_transaction(
        user,
        account,
        source=Source.manual,
        posted_date=(NOW - timedelta(days=days_old)).date(),
        **kwargs,
    )


def test_inbox_includes_old_unmatched_manual_rows_even_when_reviewed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user, simplefin_account_id="act-checking")
    cash = make_account(user, name="Cash", simplefin_account_id=None)
    _persist(db_session, account, cash)
    old = _manual(user, account, 8, reviewed=True)
    both = _manual(user, account, 9, reviewed=False)
    young = _manual(user, account, 3, reviewed=True)
    on_cash = _manual(user, cash, 20, reviewed=True)
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="bank-1",
        posted_date=(NOW - timedelta(days=30)).date(),
        reviewed=True,
    )
    _persist(db_session, old, both, young, on_cash, bank)

    page = _inbox(db_client, auth_headers)

    flags = {item["id"]: item["unmatched_manual"] for item in page["items"]}
    assert flags == {str(old.id): True, str(both.id): True}
    assert page["total"] == 2


def test_inbox_pages_over_the_union_without_repeats(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user, simplefin_account_id="act-checking")
    _persist(db_session, account)
    rows = [_manual(user, account, 10 + i, reviewed=bool(i % 2)) for i in range(4)]
    rows.append(make_transaction(user, account, reviewed=False, source=Source.simplefin))
    _persist(db_session, *rows)

    seen: list[str] = []
    params: dict[str, str] = {"limit": "2"}
    while True:
        page = _inbox(db_client, auth_headers, **params)
        assert page["total"] == 5
        seen += [item["id"] for item in page["items"]]
        if page["next_cursor"] is None:
            break
        params["cursor"] = page["next_cursor"]

    assert sorted(seen) == sorted(str(row.id) for row in rows)


def test_inbox_suggestions_ignore_split_parents(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    plain = make_transaction(
        user,
        account,
        merchant="Costco",
        reviewed=True,
        category_id=category.id,
        bucket=Bucket.personal,
        posted_date=date(2026, 9, 1),
    )
    parent = make_transaction(
        user, account, merchant="Costco", reviewed=True, posted_date=date(2026, 9, 20)
    )
    _persist(db_session, plain, parent)
    _persist(db_session, make_transaction(user, account, reviewed=True, parent_id=parent.id))
    target = make_transaction(user, account, merchant="Costco", reviewed=False)
    _persist(db_session, target)

    item = next(i for i in _inbox(db_client, auth_headers)["items"] if i["id"] == str(target.id))

    assert item["suggested_category_id"] == str(category.id)
