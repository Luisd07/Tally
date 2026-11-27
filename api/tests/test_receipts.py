"""Acceptance tests for #33: receipt upload (re-encode, size/type guards, idempotency)
and serving (owner-only, path resolved safely inside `receipts_dir`).
"""

import hashlib
import io
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import ExifTags, Image
from sqlalchemy import Engine, delete, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.deps import get_app_settings
from app.models import Receipt, Transaction, User
from app.services import receipts as receipts_service
from app.services.receipts import MAX_UPLOAD_BYTES
from tests.factories import make_account, make_receipt, make_transaction
from tests.locks import wait_until_blocked_on_lock


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _jpeg_bytes(size: tuple[int, int] = (100, 100), *, with_gps: bool = False) -> bytes:
    image = Image.new("RGB", size, "red")
    exif = image.getexif()
    if with_gps:
        exif[ExifTags.Base.Orientation] = 3
        exif[ExifTags.Base.GPSInfo] = {
            1: "N",
            2: (37.0, 46.0, 30.0),
            3: "W",
            4: (122.0, 25.0, 10.0),
        }
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _jpeg_bytes_with_comment_and_xmp() -> bytes:
    image = Image.new("RGB", (100, 100), "red")
    exif = image.getexif()
    exif[ExifTags.Base.GPSInfo] = {1: "N", 2: (37.0, 46.0, 30.0)}
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description exif:GPSLatitude="37,46.5N" '
        b'xmlns:exif="http://ns.adobe.com/exif/1.0/"/></rdf:RDF></x:xmpmeta>'
    )
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif, xmp=xmp, comment=b"GPS 37.77 -122.42 home")
    return buf.getvalue()


def _bomb_bytes(side: int = 20000) -> bytes:
    """A tiny PNG whose header declares an absurd pixel count."""
    image = Image.new("1", (side, side))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _truncated_jpeg_bytes() -> bytes:
    image = Image.effect_noise((800, 800), 50).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()[: len(buf.getvalue()) // 2]


@pytest.fixture
def transaction(db_session: Session, user: User) -> Transaction:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)
    return transaction


@pytest.fixture
def committed_transaction(engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A user, account and transaction committed to the real engine, visible to the two
    race sessions in `test_concurrent_uploads_with_the_same_receipt_id...`.
    """
    with Session(engine) as setup:
        race_user = User(display_name="Race")
        setup.add(race_user)
        setup.flush()
        account = make_account(race_user)
        setup.add(account)
        setup.flush()
        transaction = make_transaction(race_user, account)
        setup.add(transaction)
        setup.commit()
        user_id, transaction_id = race_user.id, transaction.id
    try:
        yield user_id, transaction_id
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


@pytest.fixture
def receipts_client(
    app: FastAPI, db_session: Session, settings: Settings, tmp_path: Path
) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_app_settings] = lambda: settings.model_copy(
        update={"receipts_dir": tmp_path}
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _stored_files(tmp_path: Path) -> list[Path]:
    return [p for p in tmp_path.rglob("*") if p.is_file()]


# --- POST /api/transactions/{id}/receipts -----------------------------------------------


def test_upload_reencodes_strips_gps_and_caps_the_long_edge(
    receipts_client: TestClient,
    transaction: Transaction,
    auth_headers: dict[str, str],
) -> None:
    original = _jpeg_bytes((3000, 1000), with_gps=True)

    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", original, "image/jpeg")},
        headers=auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mime"] == "image/jpeg"
    assert body["transaction_id"] == str(transaction.id)

    fetched = receipts_client.get(f"/api/receipts/{body['id']}/file", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/jpeg"
    assert fetched.headers["cache-control"] == "private, no-store"

    stored = Image.open(io.BytesIO(fetched.content))
    assert stored.format == "JPEG"
    assert max(stored.size) == 2000
    exif = stored.getexif()
    assert ExifTags.Base.GPSInfo not in exif
    assert len(exif) == 0


def test_upload_rejects_oversize_file(
    receipts_client: TestClient,
    transaction: Transaction,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    oversize = b"\x00" * (MAX_UPLOAD_BYTES + 1)

    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", oversize, "image/jpeg")},
        headers=auth_headers,
    )

    assert response.status_code == 413
    assert _stored_files(tmp_path) == []


def test_upload_rejects_non_image_bytes(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", b"not an image, just text", "image/jpeg")},
        headers=auth_headers,
    )

    assert response.status_code == 415


def test_upload_rejects_a_decompression_bomb(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("bomb.png", _bomb_bytes(), "image/png")},
        headers=auth_headers,
    )

    assert response.status_code == 415


def test_upload_rejects_a_64_megapixel_image(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    """A PNG this size is well past every per-format ceiling, JPEG's included; this is the
    shape of bomb the original finding probed with.
    """
    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("bomb.png", _bomb_bytes(side=8000), "image/png")},
        headers=auth_headers,
    )

    assert response.status_code == 415


def test_upload_accepts_a_photo_sized_jpeg_near_the_pixel_ceiling(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    """An iPhone 48 MP ProRAW-to-JPEG main-camera photo (8064x6048, ~48.8 MP) must still
    fit comfortably under JPEG's 60 MP ceiling, the one format with a scaled decode.
    """
    photo = Image.new("RGB", (8064, 6048), "red")
    buf = io.BytesIO()
    photo.save(buf, format="JPEG")

    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("photo.jpg", buf.getvalue(), "image/jpeg")},
        headers=auth_headers,
    )

    assert response.status_code == 201


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_upload_accepts_an_image_just_under_that_formats_pixel_ceiling(
    receipts_client: TestClient,
    transaction: Transaction,
    auth_headers: dict[str, str],
    fmt: str,
) -> None:
    """PNG and WebP decode at full resolution, so their ceiling is far lower than JPEG's;
    4000x4000 (16.0 M) sits just under it in both.
    """
    image = Image.new("RGBA" if fmt == "PNG" else "RGB", (4000, 4000))
    buf = io.BytesIO()
    image.save(buf, format=fmt)

    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": (f"receipt.{fmt.lower()}", buf.getvalue(), f"image/{fmt.lower()}")},
        headers=auth_headers,
    )

    assert response.status_code == 201


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_upload_rejects_an_image_just_over_that_formats_pixel_ceiling(
    receipts_client: TestClient,
    transaction: Transaction,
    auth_headers: dict[str, str],
    fmt: str,
) -> None:
    """4001x4001 (16.008 M) sits just over the 16 M PNG/WebP ceiling."""
    image = Image.new("RGBA" if fmt == "PNG" else "RGB", (4001, 4001))
    buf = io.BytesIO()
    image.save(buf, format=fmt)

    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": (f"receipt.{fmt.lower()}", buf.getvalue(), f"image/{fmt.lower()}")},
        headers=auth_headers,
    )

    assert response.status_code == 415


def test_reencode_holds_the_decode_slot_for_its_whole_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof for the module-level `_DECODE_SLOT`: while `_reencode` is partway through (a
    monkeypatched `exif_transpose` stands in for "still decoding"), a second thread's
    attempt to acquire that same semaphore must time out, not succeed. If the semaphore
    only wrapped part of the body, this attempt could slip in and run concurrently.
    """
    other_thread_acquired: list[bool] = []
    real_transpose = receipts_service.ImageOps.exif_transpose

    def slow_transpose(image: Image.Image) -> Image.Image | None:
        def try_from_other_thread() -> None:
            got = receipts_service._DECODE_SLOT.acquire(timeout=0.2)
            other_thread_acquired.append(got)
            if got:
                receipts_service._DECODE_SLOT.release()

        other = threading.Thread(target=try_from_other_thread)
        other.start()
        other.join()
        return real_transpose(image)

    monkeypatch.setattr(receipts_service.ImageOps, "exif_transpose", slow_transpose)

    receipts_service._reencode(_jpeg_bytes())

    assert other_thread_acquired == [False]


def test_upload_strips_jpeg_comment_and_xmp_alongside_exif(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", _jpeg_bytes_with_comment_and_xmp(), "image/jpeg")},
        headers=auth_headers,
    )
    body = response.json()

    fetched = receipts_client.get(f"/api/receipts/{body['id']}/file", headers=auth_headers)

    stored = Image.open(io.BytesIO(fetched.content))
    applist = getattr(stored, "applist", [])  # JPEG-only attribute, absent from the base type
    assert [segment[0] for segment in applist] == ["APP0"]
    assert dict(stored.getexif()) == {}
    assert "comment" not in stored.info
    assert "xmp" not in stored.info
    assert b"home" not in fetched.content
    assert b"GPSLatitude" not in fetched.content


def test_upload_rejects_a_truncated_jpeg(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", _truncated_jpeg_bytes(), "image/jpeg")},
        headers=auth_headers,
    )

    assert response.status_code == 415


def test_upload_rejects_formats_the_phone_never_sends(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    tiff_buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(tiff_buf, format="TIFF")
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"

    tiff_response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.tiff", tiff_buf.getvalue(), "image/tiff")},
        headers=auth_headers,
    )
    eps_response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.eps", eps, "application/postscript")},
        headers=auth_headers,
    )

    assert tiff_response.status_code == 415
    assert eps_response.status_code == 415


def test_upload_with_receipt_id_owned_by_another_user_returns_409(
    receipts_client: TestClient,
    db_session: Session,
    transaction: Transaction,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    other_transaction = make_transaction(other_user, other_account)
    _persist(db_session, other_transaction)
    theirs = make_receipt(other_user, other_transaction)
    _persist(db_session, theirs)

    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        data={"receipt_id": str(theirs.id)},
        headers=auth_headers,
    )

    assert response.status_code == 409


def test_upload_to_another_users_transaction_returns_404(
    receipts_client: TestClient,
    db_session: Session,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    theirs = make_transaction(other_user, other_account)
    _persist(db_session, theirs)

    response = receipts_client.post(
        f"/api/transactions/{theirs.id}/receipts",
        files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_idempotent_repeat_returns_the_existing_row_and_writes_no_second_file(
    receipts_client: TestClient,
    transaction: Transaction,
    auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    receipt_id = str(uuid.uuid4())

    first = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", _jpeg_bytes((400, 300)), "image/jpeg")},
        data={"receipt_id": receipt_id},
        headers=auth_headers,
    )
    second = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", _jpeg_bytes((500, 500)), "image/jpeg")},
        data={"receipt_id": receipt_id},
        headers=auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(_stored_files(tmp_path)) == 1


def test_concurrent_uploads_with_the_same_receipt_id_leave_exactly_one_file(
    engine: Engine,
    committed_transaction: tuple[uuid.UUID, uuid.UUID],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The regression for MV-1: two uploads racing on the same `receipt_id` must not let
    the loser delete the winner's file. `first` takes `_lock_receipt_id` and holds it
    uncommitted; `second` blocks on that lock for the whole call (the existence check, the
    decode, the write and the insert) and only proceeds -- straight to the cheap existing-row
    lookup, writing nothing -- once `first` commits and releases it.
    """
    user_id, transaction_id = committed_transaction
    receipt_id = uuid.uuid4()
    now = datetime.now(UTC)
    receipts_settings = settings.model_copy(update={"receipts_dir": tmp_path})

    with Session(engine) as first, Session(engine) as second:
        row_a, created_a = receipts_service.upload_receipt(
            first,
            receipts_settings,
            user_id,
            transaction_id,
            _jpeg_bytes((400, 300)),
            receipt_id,
            now,
        )
        assert created_a is True  # holds the advisory lock, uncommitted

        second_pid = second.scalar(text("SELECT pg_backend_pid()"))
        assert second_pid is not None
        result: list[tuple[Receipt, bool]] = []
        racer = threading.Thread(
            target=lambda: result.append(
                receipts_service.upload_receipt(
                    second,
                    receipts_settings,
                    user_id,
                    transaction_id,
                    _jpeg_bytes((500, 500)),
                    receipt_id,
                    now,
                )
            )
        )
        racer.start()
        wait_until_blocked_on_lock(engine, second_pid)
        first.commit()
        racer.join(timeout=10)
        second.commit()

        row_b, created_b = result[0]
        assert created_b is False
        assert row_b.id == row_a.id == receipt_id

    final_path = tmp_path / row_a.file_path
    assert final_path.exists()
    assert _stored_files(tmp_path) == [final_path]
    assert hashlib.sha256(final_path.read_bytes()).hexdigest() == row_a.sha256


def test_insert_failure_cleans_up_the_file(
    db_session: Session,
    transaction: Transaction,
    user: User,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts_settings = settings.model_copy(update={"receipts_dir": tmp_path})

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(receipts_service, "create_idempotent", boom)

    with pytest.raises(RuntimeError):
        receipts_service.upload_receipt(
            db_session,
            receipts_settings,
            user.id,
            transaction.id,
            _jpeg_bytes(),
            None,
            datetime.now(UTC),
        )

    assert _stored_files(tmp_path) == []


def test_losing_the_insert_race_discards_only_this_attempts_file(
    db_session: Session,
    transaction: Transaction,
    user: User,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-level complement to the two-session race test: forces the `created=False`
    branch directly, so the cleanup it performs is covered even without timing a real race.
    """
    receipts_settings = settings.model_copy(update={"receipts_dir": tmp_path})
    winner = make_receipt(user, transaction)
    _persist(db_session, winner)

    def fake_create_idempotent(
        db: Session, model: object, user_id: uuid.UUID, values: dict[str, object], **kwargs: object
    ) -> tuple[Receipt, bool]:
        return winner, False

    monkeypatch.setattr(receipts_service, "create_idempotent", fake_create_idempotent)

    row, created = receipts_service.upload_receipt(
        db_session,
        receipts_settings,
        user.id,
        transaction.id,
        _jpeg_bytes(),
        None,
        datetime.now(UTC),
    )

    assert created is False
    assert row is winner
    assert _stored_files(tmp_path) == []


def test_sha256_matches_the_stored_file(
    receipts_client: TestClient, transaction: Transaction, auth_headers: dict[str, str]
) -> None:
    response = receipts_client.post(
        f"/api/transactions/{transaction.id}/receipts",
        files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        headers=auth_headers,
    )
    body = response.json()

    fetched = receipts_client.get(f"/api/receipts/{body['id']}/file", headers=auth_headers)

    assert hashlib.sha256(fetched.content).hexdigest() == body["sha256"]


# --- GET /api/receipts/{id}/file ---------------------------------------------------------


def test_get_file_for_missing_and_other_users_receipt_both_404_identically(
    receipts_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    other_transaction = make_transaction(other_user, other_account)
    _persist(db_session, other_transaction)
    theirs = make_receipt(other_user, other_transaction)
    _persist(db_session, theirs)

    missing = receipts_client.get(f"/api/receipts/{uuid.uuid4()}/file", headers=auth_headers)
    someone_elses = receipts_client.get(f"/api/receipts/{theirs.id}/file", headers=auth_headers)

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


def test_get_file_refuses_a_path_traversal_attempt_via_a_tampered_row(
    receipts_client: TestClient,
    db_session: Session,
    user: User,
    transaction: Transaction,
    auth_headers: dict[str, str],
) -> None:
    tampered = make_receipt(user, transaction, file_path="../outside.jpg")
    _persist(db_session, tampered)

    response = receipts_client.get(f"/api/receipts/{tampered.id}/file", headers=auth_headers)

    assert response.status_code == 404


def test_get_file_404s_when_the_row_exists_but_the_file_is_missing(
    receipts_client: TestClient,
    db_session: Session,
    user: User,
    transaction: Transaction,
    auth_headers: dict[str, str],
) -> None:
    orphaned = make_receipt(user, transaction, file_path="2026/10/never-written.jpg")
    _persist(db_session, orphaned)

    response = receipts_client.get(f"/api/receipts/{orphaned.id}/file", headers=auth_headers)

    assert response.status_code == 404
