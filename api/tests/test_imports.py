"""#54: statement import. `PUT/GET /api/imports/mappings/{account_id}` and
`POST /api/imports/{csv,ofx}`.

Import only fills history older than the account's earliest SimpleFIN-sourced row, and
dedupes a re-upload on the fingerprint (see `app.services.imports`).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Transaction, User
from app.models.enums import AccountKind, Bucket, RunTrigger, Source
from app.services import import_parsers as parsers
from app.services import imports as service
from app.services import sync as sync_service
from tests.factories import make_account, make_import_mapping, make_rule, make_transaction
from tests.test_sync import NOW, _account_set, _connection, _transport, _txn

FIXTURES = Path(__file__).parent / "fixtures"
MAPPINGS = "/api/imports/mappings"
CSV = "/api/imports/csv"
OFX = "/api/imports/ofx"

DEBIT_CREDIT_MAPPING = {
    "date_column": "Date",
    "debit_column": "Debit",
    "credit_column": "Credit",
    "description_column": "Description",
    "date_format": "%m/%d/%Y",
}
SINGLE_MAPPING = {
    "date_column": "Posted Date",
    "amount_column": "Amount",
    "description_column": "Payee",
    "date_format": "%Y-%m-%d",
    "sign_convention": "positive_is_out",
}


def _upload(
    client: TestClient,
    url: str,
    account: Account,
    headers: dict[str, str],
    content: bytes,
    name: str = "statement.csv",
) -> Response:
    return client.post(
        url,
        params={"account_id": str(account.id)},
        files={"file": (name, content)},
        headers=headers,
    )


def _account(db_session: Session, user: User) -> Account:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    return account


def _txns(db_session: Session, account: Account) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .where(Transaction.account_id == account.id)
        .order_by(Transaction.posted_date, Transaction.amount)
    )
    return list(db_session.scalars(stmt))


# --- mappings --------------------------------------------------------------------------


def test_put_then_get_mapping(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)

    url = f"{MAPPINGS}/{account.id}"
    created = db_client.put(url, json=DEBIT_CREDIT_MAPPING, headers=auth_headers)
    assert created.status_code == 200
    assert created.json()["debit_column"] == "Debit"
    assert created.json()["sign_convention"] == "negative_is_out"

    replaced = db_client.put(url, json=SINGLE_MAPPING, headers=auth_headers)
    assert replaced.status_code == 200
    assert replaced.json()["id"] == created.json()["id"]
    assert replaced.json()["debit_column"] is None

    fetched = db_client.get(url, headers=auth_headers)
    assert fetched.json() == replaced.json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_column": "Amount"},  # both modes
        {"credit_column": None},  # debit without credit
        {"debit_column": None, "credit_column": None},  # neither mode
        {"date_format": "%Q"},
        {"date_format": "%m/%d"},
        {"has_header": False},  # names, not indexes
    ],
)
def test_put_mapping_rejects_inconsistent_body(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    overrides: dict[str, object],
) -> None:
    account = _account(db_session, user)
    body = {**DEBIT_CREDIT_MAPPING, **overrides}
    response = db_client.put(f"{MAPPINGS}/{account.id}", json=body, headers=auth_headers)
    assert response.status_code == 422


def test_get_mapping_missing_is_404(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    assert db_client.get(f"{MAPPINGS}/{account.id}", headers=auth_headers).status_code == 404


def test_mapping_of_a_foreign_account_is_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = _account(db_session, other_user)
    db_session.add(make_import_mapping(other_user, theirs))
    db_session.flush()

    assert db_client.get(f"{MAPPINGS}/{theirs.id}", headers=auth_headers).status_code == 404
    put = db_client.put(f"{MAPPINGS}/{theirs.id}", json=SINGLE_MAPPING, headers=auth_headers)
    assert put.status_code == 404
    assert db_client.get(f"{MAPPINGS}/{uuid.uuid4()}", headers=auth_headers).status_code == 404


# --- CSV -------------------------------------------------------------------------------


def test_csv_debit_credit_columns(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, amount_column=None, **DEBIT_CREDIT_MAPPING))
    db_session.flush()

    response = _upload(
        db_client, CSV, account, auth_headers, (FIXTURES / "bank_debit_credit.csv").read_bytes()
    )

    assert response.status_code == 200
    assert response.json() == {
        "inserted": 4,
        "skipped_duplicate": 0,
        "skipped_synced_range": 0,
        "errors": [],
    }
    rows = _txns(db_session, account)
    assert [(r.posted_date, r.amount, r.description_raw) for r in rows] == [
        (date(2026, 9, 3), Decimal("-120.50"), "HERTZ RENTAL CAR"),
        (date(2026, 9, 4), Decimal("2000.00"), "PAYROLL DEPOSIT"),
        (date(2026, 9, 5), Decimal("-4.25"), "COFFEE SHOP"),
        (date(2026, 9, 5), Decimal("-4.25"), "COFFEE SHOP"),
    ]
    assert all(r.source == Source.csv and not r.pending and r.fingerprint for r in rows)


def test_csv_single_amount_column_flips_sign_and_reports_bad_rows(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()

    response = _upload(
        db_client, CSV, account, auth_headers, (FIXTURES / "card_single_amount.csv").read_bytes()
    )

    body = response.json()
    assert response.status_code == 200
    assert body["inserted"] == 2
    assert body["errors"] == [
        {"row": 4, "message": "invalid date"},
        {"row": 5, "message": "invalid amount 'abc'"},
        {"row": 6, "message": "missing description"},
        {"row": 8, "message": "missing amount"},
    ]
    amounts = {r.description_raw: r.amount for r in _txns(db_session, account)}
    assert amounts == {
        "AMAZON MKTPLACE, INC": Decimal("-45.99"),
        "PAYMENT THANK YOU": Decimal("1200.00"),
    }


def test_csv_reupload_inserts_nothing(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, amount_column=None, **DEBIT_CREDIT_MAPPING))
    db_session.flush()
    content = (FIXTURES / "bank_debit_credit.csv").read_bytes()

    _upload(db_client, CSV, account, auth_headers, content)
    again = _upload(db_client, CSV, account, auth_headers, content)

    assert again.json()["inserted"] == 0
    assert again.json()["skipped_duplicate"] == 4
    assert len(_txns(db_session, account)) == 4


def test_csv_only_fills_before_the_earliest_synced_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, amount_column=None, **DEBIT_CREDIT_MAPPING))
    db_session.add(
        make_transaction(
            user,
            account,
            posted_date=date(2026, 9, 5),
            source=Source.simplefin,
            external_id="sf-1",
            description_raw="Coffee Shop #12",
        )
    )
    db_session.flush()

    response = _upload(
        db_client, CSV, account, auth_headers, (FIXTURES / "bank_debit_credit.csv").read_bytes()
    )

    assert response.json()["inserted"] == 2
    assert response.json()["skipped_synced_range"] == 2
    csv_dates = [r.posted_date for r in _txns(db_session, account) if r.source == Source.csv]
    assert csv_dates == [date(2026, 9, 3), date(2026, 9, 4)]


def test_csv_applies_rules(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add_all(
        [
            make_import_mapping(user, account, amount_column=None, **DEBIT_CREDIT_MAPPING),
            make_rule(user, pattern="HERTZ", set_merchant="Hertz", set_bucket=Bucket.reimbursable),
        ]
    )
    db_session.flush()

    content = (FIXTURES / "bank_debit_credit.csv").read_bytes()
    _upload(db_client, CSV, account, auth_headers, content)

    hertz = next(r for r in _txns(db_session, account) if "HERTZ" in r.description_raw)
    assert (hertz.merchant, hertz.bucket, hertz.reviewed) == ("Hertz", Bucket.reimbursable, True)


def test_an_imported_card_payment_pairs_with_a_synced_checking_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    checking = _account(db_session, user)
    card = make_account(user, name="Card", kind=AccountKind.credit_card)
    db_session.add(card)
    db_session.flush()
    db_session.add(make_import_mapping(user, card, **SINGLE_MAPPING))
    out = make_transaction(
        user,
        checking,
        posted_date=date(2026, 9, 3),
        amount=Decimal("-500.00"),
        source=Source.simplefin,
        external_id="sf-pay",
    )
    db_session.add(out)
    db_session.flush()

    content = b"Posted Date,Amount,Payee\n2026-09-06,-500.00,PAYMENT THANK YOU\n"
    response = _upload(db_client, CSV, card, auth_headers, content)

    assert response.json()["inserted"] == 1
    db_session.expire_all()
    inn = _txns(db_session, card)[0]
    assert inn.source == Source.csv and inn.amount == Decimal("500.00")
    assert out.bucket == inn.bucket == Bucket.transfer
    assert out.transfer_pair_id == inn.id
    assert inn.transfer_pair_id == out.id


def test_csv_without_a_mapping_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    response = _upload(db_client, CSV, account, auth_headers, b"a,b\n1,2\n")
    assert response.status_code == 422


def test_csv_header_missing_a_mapped_column_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, amount_column=None, **DEBIT_CREDIT_MAPPING))
    db_session.flush()
    response = _upload(db_client, CSV, account, auth_headers, b"When,What\n1,2\n")
    assert response.status_code == 422
    assert not _txns(db_session, account)


def test_csv_without_header_uses_indexes(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(
        make_import_mapping(
            user,
            account,
            date_column="0",
            amount_column="1",
            description_column="2",
            date_format="%Y-%m-%d",
            has_header=False,
        )
    )
    db_session.flush()
    response = _upload(db_client, CSV, account, auth_headers, b"2026-01-02,(9.50),LUNCH\n")
    assert response.json()["inserted"] == 1
    assert _txns(db_session, account)[0].amount == Decimal("-9.50")


def test_csv_row_cap(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()
    over = b"Posted Date,Amount,Payee\n" + b"2026-01-02,1.00,X\n" * (parsers.MAX_ROWS + 1)
    response = _upload(db_client, CSV, account, auth_headers, over)
    assert response.status_code == 422
    assert not _txns(db_session, account)


def test_upload_size_cap(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    big = b"x" * (service.MAX_FILE_BYTES + 1)
    assert _upload(db_client, CSV, account, auth_headers, big).status_code == 413
    assert _upload(db_client, OFX, account, auth_headers, big).status_code == 413


@pytest.mark.parametrize("url", [CSV, OFX])
def test_upload_to_unknown_or_foreign_account_is_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
    url: str,
) -> None:
    theirs = _account(db_session, other_user)
    db_session.add(make_import_mapping(other_user, theirs, **SINGLE_MAPPING))
    db_session.flush()
    unknown = make_account(user)
    unknown.id = uuid.uuid4()

    assert _upload(db_client, url, theirs, auth_headers, b"x").status_code == 404
    assert _upload(db_client, url, unknown, auth_headers, b"x").status_code == 404


# --- OFX/QFX ---------------------------------------------------------------------------


def test_qfx_import_and_reupload(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    content = (FIXTURES / "statement.qfx").read_bytes()

    first = _upload(db_client, OFX, account, auth_headers, content, "statement.qfx")

    assert first.status_code == 200
    assert first.json() == {
        "inserted": 4,
        "skipped_duplicate": 0,
        "skipped_synced_range": 0,
        "errors": [{"row": 5, "message": "invalid date"}],
    }
    rows = _txns(db_session, account)
    assert [(r.posted_date, r.amount, r.description_raw) for r in rows] == [
        (date(2026, 9, 3), Decimal("-120.50"), "HERTZ RENTAL CAR RENTAL"),
        (date(2026, 9, 4), Decimal("2000.00"), "PAYROLL DEPOSIT"),
        (date(2026, 9, 5), Decimal("-4.25"), "COFFEE SHOP"),
        (date(2026, 9, 5), Decimal("-4.25"), "COFFEE SHOP"),
    ]
    assert all(r.source == Source.csv for r in rows)

    again = _upload(db_client, OFX, account, auth_headers, content, "statement.qfx")
    assert again.json()["inserted"] == 0
    assert again.json()["skipped_duplicate"] == 4


def test_ofx_xml_dialect(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    xml = (
        b"<?xml version='1.0'?><OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>"
        b"<STMTTRN><DTPOSTED>20260801</DTPOSTED><TRNAMT>-3,50</TRNAMT><NAME>TOLL</NAME></STMTTRN>"
        b"</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )
    response = _upload(db_client, OFX, account, auth_headers, xml, "s.ofx")
    assert response.json()["inserted"] == 1
    assert _txns(db_session, account)[0].amount == Decimal("-3.50")


def test_ofx_rejects_a_non_ofx_file(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    assert _upload(db_client, OFX, account, auth_headers, b"a,b,c\n").status_code == 422


def test_ofx_only_fills_before_the_earliest_synced_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(
        make_transaction(
            user, account, posted_date=date(2026, 9, 4), source=Source.simplefin, external_id="x"
        )
    )
    db_session.flush()
    response = _upload(
        db_client, OFX, account, auth_headers, (FIXTURES / "statement.qfx").read_bytes(), "s.qfx"
    )
    assert response.json()["inserted"] == 1
    assert response.json()["skipped_synced_range"] == 3


def test_import_routes_require_auth(client: TestClient) -> None:
    params = {"account_id": str(uuid.uuid4())}
    assert client.post(CSV, params=params, files={"file": ("a.csv", b"x")}).status_code == 401
    assert client.get(f"{MAPPINGS}/{uuid.uuid4()}").status_code == 401


# --- hostile input, locking, and a later sync ------------------------------------------


@pytest.mark.parametrize("amount", ["99999999999999999999999999999", "1E+30", "-1E+999999999"])
def test_csv_out_of_range_amount_is_a_row_error(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    amount: str,
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()
    content = f"Posted Date,Amount,Payee\n2026-01-02,{amount},BIG\n2026-01-03,-1.00,OK\n"

    response = _upload(db_client, CSV, account, auth_headers, content.encode())

    assert response.status_code == 200
    body = response.json()
    assert body["inserted"] == 1
    assert [e["row"] for e in body["errors"]] == [2]
    assert "out of range" in body["errors"][0]["message"]


def test_ofx_out_of_range_amount_is_a_row_error(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    ofx = (
        b"<OFX><BANKTRANLIST>"
        b"<STMTTRN><DTPOSTED>20260801</DTPOSTED><TRNAMT>1E+999999999<NAME>BIG</STMTTRN>"
        b"<STMTTRN><DTPOSTED>20260802</DTPOSTED><TRNAMT>1E+30<NAME>BIG</STMTTRN>"
        b"<STMTTRN><DTPOSTED>20260803</DTPOSTED><TRNAMT>-2.00<NAME>OK</STMTTRN>"
        b"</BANKTRANLIST></OFX>"
    )

    response = _upload(db_client, OFX, account, auth_headers, ofx, "s.ofx")

    assert response.status_code == 200
    body = response.json()
    assert body["inserted"] == 1
    assert [e["row"] for e in body["errors"]] == [1, 2]


def test_nul_bytes_are_stripped_from_csv_and_ofx(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()
    csv_body = b"Posted Date,Amount,Payee\n2026-01-02,-1.00,a\x00b\n"
    ofx = b"<OFX><STMTTRN><DTPOSTED>20260801</DTPOSTED><TRNAMT>-3.00<NAME>c\x00d</STMTTRN></OFX>"

    assert _upload(db_client, CSV, account, auth_headers, csv_body).status_code == 200
    assert _upload(db_client, OFX, account, auth_headers, ofx, "s.ofx").status_code == 200

    assert [r.description_raw for r in _txns(db_session, account)] == ["ab", "cd"]


@pytest.mark.parametrize("kind", ["csv", "ofx"])
def test_imports_take_the_per_user_lock(
    db_session: Session, user: User, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    account = _account(db_session, user)
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()
    locked: list[object] = []
    monkeypatch.setattr(service, "lock_user", lambda db, user_id: locked.append(user_id))

    if kind == "csv":
        service.import_csv(db_session, user.id, account.id, b"Posted Date,Amount,Payee\n")
    else:
        service.import_ofx(db_session, user.id, account.id, b"<OFX></OFX>")

    assert locked == [user.id]


def test_a_backfill_after_a_csv_import_absorbs_into_the_csv_rows(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add_all([account, _connection(user, settings)])
    db_session.flush()
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()
    content = b"Posted Date,Amount,Payee\n2026-03-10,20.00,CAFE\n2026-03-20,35.00,MARKET\n"
    assert service.import_csv(db_session, user.id, account.id, content).inserted == 2
    csv_ids = {r.id for r in _txns(db_session, account)}

    def at(day: int) -> int:
        return int(datetime(2026, 3, day, 12, tzinfo=UTC).timestamp())

    payload = _account_set(
        [
            {
                "id": "act-checking",
                "name": "Checking",
                "balance": "1.00",
                "balance-date": int(NOW.timestamp()),
                "transactions": [
                    _txn(
                        "sf-0",
                        int(datetime(2026, 2, 15, 12, tzinfo=UTC).timestamp()),
                        "-9.99",
                        "EARLIER",
                    ),
                    _txn("sf-1", at(11), "-20.00", "CAFE #12 SEATTLE"),
                    _txn("sf-2", at(21), "-35.00", "MARKET 0042"),
                ],
            }
        ]
    )
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    db_session.expire_all()
    rows = [r for r in _txns(db_session, account) if r.external_id != "sf-0"]
    assert {r.id for r in rows} == csv_ids
    assert {r.external_id for r in rows} == {"sf-1", "sf-2"}
    assert all(r.source == Source.simplefin for r in rows)


def _import_then_sync(
    db_session: Session, user: User, settings: Settings, content: bytes, txns: list[dict]
) -> tuple[Account, list[Transaction]]:
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add_all([account, _connection(user, settings)])
    db_session.flush()
    db_session.add(make_import_mapping(user, account, **SINGLE_MAPPING))
    db_session.flush()
    service.import_csv(db_session, user.id, account.id, content)
    payload = _account_set(
        [
            {
                "id": "act-checking",
                "name": "Checking",
                "balance": "1.00",
                "balance-date": int(NOW.timestamp()),
                "transactions": txns,
            }
        ]
    )
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )
    db_session.expire_all()
    return account, _txns(db_session, account)


def _at(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, 18, tzinfo=UTC).timestamp())


def test_a_charge_before_the_sync_window_is_not_absorbed_into_a_later_one(
    db_session: Session, user: User, settings: Settings
) -> None:
    content = b"Posted Date,Amount,Payee\n2025-12-30,4.50,COFFEE BAR\n"
    txns = [_txn("sf-1", _at(2026, 1, 2), "-4.50", "COFFEE BAR #9")]

    _, rows = _import_then_sync(db_session, user, settings, content, txns)

    assert [(r.posted_date, r.source) for r in rows] == [
        (date(2025, 12, 30), Source.csv),
        (date(2026, 1, 2), Source.simplefin),
    ]


def test_two_same_amount_charges_imported_and_backfilled_end_as_two_rows(
    db_session: Session, user: User, settings: Settings
) -> None:
    content = b"Posted Date,Amount,Payee\n2026-03-10,4.50,COFFEE\n2026-03-12,4.50,COFFEE\n"
    txns = [
        _txn("sf-1", _at(2026, 3, 10), "-4.50", "COFFEE #1"),
        _txn("sf-2", _at(2026, 3, 12), "-4.50", "COFFEE #1"),
    ]

    _, rows = _import_then_sync(db_session, user, settings, content, txns)

    assert [(r.posted_date, r.source) for r in rows] == [
        (date(2026, 3, 10), Source.simplefin),
        (date(2026, 3, 12), Source.simplefin),
    ]


def test_reuploading_a_statement_after_a_backfill_absorbed_its_rows_inserts_nothing(
    db_session: Session, user: User, settings: Settings
) -> None:
    content = b"Posted Date,Amount,Payee\n2026-03-10,20.00,CAFE\n2026-03-20,35.00,MARKET\n"
    txns = [
        _txn("sf-0", _at(2026, 2, 15), "-9.99", "EARLIER"),
        _txn("sf-1", _at(2026, 3, 11), "-20.00", "CAFE #12"),
        _txn("sf-2", _at(2026, 3, 21), "-35.00", "MARKET 0042"),
    ]
    account, rows = _import_then_sync(db_session, user, settings, content, txns)
    assert all(r.source == Source.simplefin for r in rows)

    result = service.import_csv(db_session, user.id, account.id, content)

    assert result.inserted == 0
    assert len(_txns(db_session, account)) == 3


def test_a_charge_before_the_banks_history_start_is_not_absorbed_into_a_later_one(
    db_session: Session, user: User, settings: Settings
) -> None:
    content = b"Posted Date,Amount,Payee\n2026-02-27,4.50,COFFEE\n"
    txns = [_txn("sf-1", _at(2026, 3, 1), "-4.50", "COFFEE #1")]

    _, rows = _import_then_sync(db_session, user, settings, content, txns)

    assert [(r.posted_date, r.source) for r in rows] == [
        (date(2026, 2, 27), Source.csv),
        (date(2026, 3, 1), Source.simplefin),
    ]


def test_a_pair_straddling_the_banks_history_start_stays_two_rows_and_reupload_adds_none(
    db_session: Session, user: User, settings: Settings
) -> None:
    content = b"Posted Date,Amount,Payee\n2026-10-09,20.00,CAFE\n"
    txns = [_txn("sf-1", _at(2026, 10, 10), "-20.00", "CAFE #12")]

    account, rows = _import_then_sync(db_session, user, settings, content, txns)
    assert [(r.posted_date, r.source) for r in rows] == [
        (date(2026, 10, 9), Source.csv),
        (date(2026, 10, 10), Source.simplefin),
    ]

    result = service.import_csv(db_session, user.id, account.id, content)

    assert result.inserted == 0
    assert len(_txns(db_session, account)) == 2
