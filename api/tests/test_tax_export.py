"""Acceptance tests for #72's server half: `GET /api/tax/export` streams a zip of the
year's business transactions (CSV) and their receipt files.
"""

import csv
import io
import zipfile
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.deps import get_app_settings
from app.models import User
from app.models.enums import Bucket
from app.services.tax_export import plan_export, stream_zip
from tests.factories import (
    make_account,
    make_category,
    make_mileage_rate,
    make_mileage_trip,
    make_odometer_reading,
    make_receipt,
    make_transaction,
)

EXPORT = "/api/tax/export"
SUMMARY = "/api/tax/summary"
HEADER = [
    "date",
    "merchant",
    "description",
    "amount",
    "category",
    "tax_line",
    "deductible_pct",
    "bucket",
    "receipts",
]


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


@pytest.fixture
def export_client(
    app: FastAPI, db_session: Session, settings: Settings, tmp_path: Path
) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_app_settings] = lambda: settings.model_copy(
        update={"receipts_dir": tmp_path, "reimbursements_on_1099": False}
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _zip(response_content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(response_content))


def _csv_rows(archive: zipfile.ZipFile, year: int = 2026) -> list[dict[str, str]]:
    text = archive.read(f"transactions-{year}.csv").decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _mileage_lines(archive: zipfile.ZipFile, year: int = 2026) -> list[list[str]]:
    raw = archive.read(f"mileage-{year}.csv")
    assert raw.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


def _write_receipt_file(tmp_path: Path, relative: str, data: bytes = b"jpegbytes") -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_zip_has_csv_and_receipts_and_totals_match_the_summary(
    export_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    account = make_account(user)
    category = make_category(user, tax_line="Office expense", deductible_pct=100)
    _persist(db_session, account, category)
    with_receipt = make_transaction(
        user,
        account,
        bucket=Bucket.business,
        merchant="Staples",
        amount=Decimal("-40.00"),
        category_id=category.id,
    )
    uncategorized = make_transaction(
        user, account, bucket=Bucket.business, merchant="Cafe", amount=Decimal("-10.50")
    )
    personal = make_transaction(user, account, bucket=Bucket.personal)
    pending = make_transaction(user, account, bucket=Bucket.business, pending=True)
    reimbursable = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, with_receipt, uncategorized, personal, pending, reimbursable)
    receipt = make_receipt(user, with_receipt, file_path="2026/10/a.jpg")
    _persist(db_session, receipt)
    _write_receipt_file(tmp_path, "2026/10/a.jpg", b"IMG")

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="tally-tax-2026.zip"'
    archive = _zip(response.content)
    expected_name = f"receipts/2026-10-01_staples_{str(receipt.id)[:8]}.jpg"
    assert sorted(archive.namelist()) == sorted(
        ["transactions-2026.csv", "mileage-2026.csv", expected_name]
    )
    assert archive.read(expected_name) == b"IMG"
    assert archive.read("transactions-2026.csv").decode("utf-8-sig").splitlines()[0] == ",".join(
        HEADER
    )

    rows = _csv_rows(archive)
    assert len(rows) == 2
    by_merchant = {r["merchant"]: r for r in rows}
    assert by_merchant["Staples"]["receipts"] == expected_name
    assert by_merchant["Staples"]["tax_line"] == "Office expense"
    assert by_merchant["Staples"]["deductible_pct"] == "100"
    assert by_merchant["Cafe"]["receipts"] == ""

    summary = export_client.get(SUMMARY, params={"year": 2026}, headers=auth_headers).json()
    summary_spent = sum(Decimal(line["spent"]) for line in summary["expenses_by_line"]) + Decimal(
        summary["uncategorized_business"]["spent"]
    )
    assert -sum(Decimal(r["amount"]) for r in rows) == summary_spent


def test_reimbursable_rows_follow_the_1099_setting(
    app: FastAPI,
    db_session: Session,
    settings: Settings,
    user: User,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    _persist(db_session, make_transaction(user, account, bucket=Bucket.reimbursable))
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_app_settings] = lambda: settings.model_copy(
        update={"receipts_dir": tmp_path, "reimbursements_on_1099": True}
    )
    with TestClient(app) as c:
        response = c.get(EXPORT, params={"year": 2026}, headers=auth_headers)
    app.dependency_overrides.clear()

    assert [r["bucket"] for r in _csv_rows(_zip(response.content))] == ["reimbursable"]


def test_split_children_are_exported_with_the_parents_receipt(
    export_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(
        user, account, bucket=Bucket.business, merchant="Costco", amount=Decimal("-100.00")
    )
    _persist(db_session, parent)
    child_a = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-60.00"), parent_id=parent.id
    )
    child_b = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-40.00"), parent_id=parent.id
    )
    receipt = make_receipt(user, parent, file_path="2026/10/p.jpg")
    _persist(db_session, child_a, child_b, receipt)
    _write_receipt_file(tmp_path, "2026/10/p.jpg")

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    archive = _zip(response.content)
    rows = _csv_rows(archive)
    assert sorted(r["amount"] for r in rows) == ["-40.00", "-60.00"]
    assert len({r["receipts"] for r in rows}) == 1
    assert rows[0]["receipts"].startswith("receipts/")
    assert len([n for n in archive.namelist() if n.startswith("receipts/")]) == 1


def test_missing_receipt_file_is_listed_in_missing_txt(
    export_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.business, merchant="Gone")
    _persist(db_session, transaction)
    receipt = make_receipt(user, transaction, file_path="2026/10/nope.jpg")
    _persist(db_session, receipt)

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    archive = _zip(response.content)
    assert sorted(archive.namelist()) == [
        "mileage-2026.csv",
        "missing.txt",
        "transactions-2026.csv",
    ]
    assert str(receipt.id) in archive.read("missing.txt").decode()
    assert _csv_rows(archive)[0]["receipts"] == ""


def test_a_receipt_deleted_after_planning_is_listed_in_missing_txt(
    db_session: Session, settings: Settings, user: User, tmp_path: Path
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.business, merchant="Vanishing")
    _persist(db_session, transaction)
    receipt = make_receipt(user, transaction, file_path="2026/10/soon-gone.jpg")
    _persist(db_session, receipt)
    _write_receipt_file(tmp_path, "2026/10/soon-gone.jpg")
    plan = plan_export(
        db_session, user.id, 2026, settings.model_copy(update={"receipts_dir": tmp_path})
    )
    assert len(plan.files) == 1
    (tmp_path / "2026/10/soon-gone.jpg").unlink()

    archive = _zip(b"".join(stream_zip(plan)))

    assert archive.testzip() is None
    assert sorted(archive.namelist()) == [
        "mileage-2026.csv",
        "missing.txt",
        "transactions-2026.csv",
    ]
    assert str(receipt.id) in archive.read("missing.txt").decode()


def test_stream_zip_yields_a_large_receipt_in_several_chunks(
    db_session: Session, settings: Settings, user: User, tmp_path: Path
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.business, merchant="Big")
    _persist(db_session, transaction)
    _persist(db_session, make_receipt(user, transaction, file_path="2026/10/big.jpg"))
    data = bytes(range(256)) * 1024  # 256 KiB
    _write_receipt_file(tmp_path, "2026/10/big.jpg", data)
    plan = plan_export(
        db_session, user.id, 2026, settings.model_copy(update={"receipts_dir": tmp_path})
    )

    chunks = [c for c in stream_zip(plan) if c]

    assert len(chunks) > 1
    archive = _zip(b"".join(chunks))
    assert archive.testzip() is None
    assert [archive.read(n) for n in archive.namelist() if n.startswith("receipts/")] == [data]


def test_another_users_rows_and_receipts_are_never_included(
    export_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    mine_account = make_account(user)
    theirs_account = make_account(other_user)
    _persist(db_session, mine_account, theirs_account)
    mine = make_transaction(user, mine_account, bucket=Bucket.business, merchant="Mine")
    theirs = make_transaction(other_user, theirs_account, bucket=Bucket.business, merchant="Theirs")
    _persist(db_session, mine, theirs)
    # A foreign receipt pointing at the caller's own transaction must not leak either.
    foreign_on_mine = make_receipt(other_user, mine, file_path="2026/10/f1.jpg")
    their_receipt = make_receipt(other_user, theirs, file_path="2026/10/f2.jpg")
    _persist(db_session, foreign_on_mine, their_receipt)
    _write_receipt_file(tmp_path, "2026/10/f1.jpg")
    _write_receipt_file(tmp_path, "2026/10/f2.jpg")

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    archive = _zip(response.content)
    assert archive.namelist() == ["transactions-2026.csv", "mileage-2026.csv"]
    assert [r["merchant"] for r in _csv_rows(archive)] == ["Mine"]


def test_merchant_text_cannot_traverse_out_of_receipts(
    export_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(
        user, account, bucket=Bucket.business, merchant="../../etc/passwd"
    )
    _persist(db_session, transaction)
    receipt = make_receipt(user, transaction, file_path="2026/10/t.jpg")
    _persist(db_session, receipt)
    _write_receipt_file(tmp_path, "2026/10/t.jpg")

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    names = [n for n in _zip(response.content).namelist() if n.startswith("receipts/")]
    assert len(names) == 1
    assert names[0] == f"receipts/2026-10-01_etc-passwd_{str(receipt.id)[:8]}.jpg"
    assert ".." not in names[0]


def test_a_receipt_path_outside_the_receipts_dir_is_treated_as_missing(
    export_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    (tmp_path.parent / "outside-secret.txt").write_text("secret")
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.business)
    _persist(db_session, transaction)
    _persist(db_session, make_receipt(user, transaction, file_path="../outside-secret.txt"))

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    assert "missing.txt" in _zip(response.content).namelist()
    assert not any(n.startswith("receipts/") for n in _zip(response.content).namelist())


def test_mileage_csv_lists_the_years_trips_and_matches_the_summary(
    export_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    _persist(
        db_session,
        make_mileage_rate(user, effective_from=date(2026, 1, 1), rate=Decimal("0.7250")),
        make_mileage_rate(user, effective_from=date(2026, 7, 1), rate=Decimal("0.7600")),
        make_mileage_trip(
            user,
            date=date(2026, 7, 1),
            miles=Decimal("12.3"),
            purpose="=Track day",
            start_location="Office",
            end_location="Daytona",
        ),
        make_mileage_trip(user, date=date(2026, 6, 30), miles=Decimal("10.0"), purpose="Setup"),
        make_mileage_trip(user, date=date(2025, 12, 31), miles=Decimal("99.0")),
        make_mileage_trip(other_user, date=date(2026, 5, 1), miles=Decimal("50.0")),
        make_odometer_reading(
            user, start_reading=Decimal("41250.0"), end_reading=Decimal("52000.5")
        ),
    )

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    lines = _mileage_lines(_zip(response.content))
    assert lines[0] == ["date", "miles", "purpose", "start", "end", "rate", "amount"]
    assert lines[1] == ["2026-06-30", "10.0", "Setup", "", "", "0.7250", "7.25"]
    assert lines[2] == ["2026-07-01", "12.3", "'=Track day", "Office", "Daytona", "0.7600", "9.35"]
    assert lines[3] == []
    totals = {label: value for label, value in lines[4:]}
    assert totals == {
        "business miles": "22.3",
        "mileage amount": "16.60",
        "odometer start": "41250.0",
        "odometer end": "52000.5",
        "total miles": "10750.5",
    }
    summary = export_client.get(SUMMARY, params={"year": 2026}, headers=auth_headers).json()
    assert totals["mileage amount"] == summary["mileage"]["mileage_amount"]
    assert totals["business miles"] == summary["mileage"]["business_miles"]


def test_mileage_csv_without_trips_or_odometer_has_only_empty_totals(
    export_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    assert _mileage_lines(_zip(response.content)) == [
        ["date", "miles", "purpose", "start", "end", "rate", "amount"],
        [],
        ["business miles", "0.0"],
        ["mileage amount", "0.00"],
        ["odometer start", ""],
        ["odometer end", ""],
        ["total miles", ""],
    ]


def test_actual_car_cost_rows_show_zero_deductible_pct(
    export_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    gas = make_category(user, name="Gas", tax_line="car_truck", deductible_pct=100)
    parking = make_category(user, name="Parking", tax_line="car_parking_tolls", deductible_pct=100)
    _persist(db_session, account, gas, parking)
    _persist(
        db_session,
        make_transaction(
            user, account, bucket=Bucket.business, merchant="Shell", category_id=gas.id
        ),
        make_transaction(
            user, account, bucket=Bucket.business, merchant="Garage", category_id=parking.id
        ),
    )

    response = export_client.get(EXPORT, params={"year": 2026}, headers=auth_headers)

    by_merchant = {r["merchant"]: r for r in _csv_rows(_zip(response.content))}
    assert by_merchant["Shell"]["deductible_pct"] == "0"
    assert by_merchant["Garage"]["deductible_pct"] == "100"


@pytest.mark.parametrize("year", ["1899", "2101", "abc"])
def test_a_bad_year_is_422(
    export_client: TestClient, auth_headers: dict[str, str], year: str
) -> None:
    response = export_client.get(EXPORT, params={"year": year}, headers=auth_headers)

    assert response.status_code == 422
