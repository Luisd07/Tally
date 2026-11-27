"""Receipt upload and serving: re-encode on the way in, scope and resolve safely on the way out.

See `.claude/rules/api.md` for user scoping and `app.services.crud` for the idempotent-create
pattern shared with accounts/categories/transactions.
"""

import hashlib
import io
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger
from PIL import Image, ImageOps
from PIL.Image import DecompressionBombError, DecompressionBombWarning, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import Conflict, NotFound, PayloadTooLarge, UnsupportedMediaType
from app.models import Receipt, Transaction
from app.scoping import get_owned
from app.services.crud import create_idempotent

# Caddy's `max_size 15MB` is SI (15,000,000 bytes, including multipart overhead); keep the
# app's own cap at or under that so an oversize upload gets this route's JSON 413 instead
# of Caddy's bare one.
MAX_UPLOAD_BYTES = 15_000_000
MAX_LONG_EDGE = 2000
JPEG_QUALITY = 85

# JPEG decodes at a draft-reduced scale (see `draft()` below), so a 48 MP iPhone photo
# (8064x6048, the ProRAW main-camera output) costs about as much memory as a 2000px-long-edge
# image, not a full-resolution one. PNG and WebP have no scaled-decode path in Pillow --
# whatever their pixel count, the full buffer gets decoded before anything else can shrink
# it -- so their ceiling has to bound a full-resolution decode directly. 16 MP covers a 4K
# screenshot (3840x2160 = 8.3M), a 5K display screenshot (5120x2880 = 14.7M) and a scanned US
# Letter page at up to ~400dpi (3400x4400 = 14.96M), which is what a PNG/WebP upload is
# expected to be; phone camera photos arrive as JPEG (or HEIC-as-JPEG per #35).
MAX_INPUT_PIXELS = {"JPEG": 60_000_000, "PNG": 16_000_000, "WEBP": 16_000_000}
# Pillow's own guard, as a backstop below the highest per-format ceiling above.
Image.MAX_IMAGE_PIXELS = max(MAX_INPUT_PIXELS.values())

ONLY_FORMATS = list(MAX_INPUT_PIXELS)  # what the phone's camera, scanner and photo library send

# Anything Pillow can't decode cleanly, including a file whose declared pixel count is a
# decompression bomb: `UnidentifiedImageError` (unknown/disallowed format), `OSError` and
# `SyntaxError` (corrupt or truncated data surfaced by `.load()`), `ValueError` (a few
# decoders raise this for malformed headers), and `DecompressionBombError`/-`Warning` (the
# latter only reachable under a global `warnings.filterwarnings("error")` such as this
# project's own `pytest.ini_options.filterwarnings`; in production it's never raised, since
# the per-format ceiling above already rejects anything that would trigger it).
_UNUSABLE_IMAGE_ERRORS = (
    UnidentifiedImageError,
    DecompressionBombError,
    DecompressionBombWarning,
    OSError,
    ValueError,
    SyntaxError,
)

# One decode/re-encode at a time, process-wide: a FastAPI sync route runs in a threadpool,
# and Pillow's decode is the only part of this request with real memory cost. Capping that
# to one in flight bounds worst-case peak memory to a single image's, no matter how many
# uploads land at once.
_DECODE_SLOT = threading.BoundedSemaphore(1)


def _reencode(data: bytes) -> bytes:
    """Re-encode `data` as a stripped, size-capped JPEG.

    Raises `UnsupportedMediaType` for anything Pillow can't open as a `JPEG`/`PNG`/`WEBP`
    (including HEIC, which needs pillow-heif to decode -- not added here, see #35), for an
    image over that format's entry in `MAX_INPUT_PIXELS`, and for a corrupt or truncated file.
    """
    with _DECODE_SLOT:
        try:
            image = Image.open(io.BytesIO(data), formats=ONLY_FORMATS)
            # `formats=` above guarantees one of the three keys below, never None.
            assert image.format is not None
            width, height = image.size
            if width * height > MAX_INPUT_PIXELS[image.format]:
                raise UnsupportedMediaType("Image is too large.")
            if image.format == "JPEG":
                # Decode straight to roughly the target size instead of full resolution --
                # the fix for the ~480 MB peak a full-size 48 MP JPEG otherwise costs.
                image.draft("RGB", (MAX_LONG_EDGE, MAX_LONG_EDGE))
            image.load()
            transposed = ImageOps.exif_transpose(image)
            if transposed is not None:
                image = transposed
            image = image.convert("RGB")
        except _UNUSABLE_IMAGE_ERRORS as exc:
            raise UnsupportedMediaType("The file isn't a supported image.") from exc

        if max(image.size) > MAX_LONG_EDGE:
            image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)

        # Rather than pass `exif=`/`comment=`/`xmp=` kwargs to `save`, drop whatever Pillow
        # carried forward in `image.info` (EXIF, XMP, and the JPEG COM segment all live there)
        # so the output is built from pixels alone.
        image.info.clear()
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()


def _write_atomically(final_path: Path, data: bytes) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=final_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, final_path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _lock_receipt_id(db: Session, receipt_id: uuid.UUID) -> None:
    """Block until this transaction holds the advisory lock for `receipt_id`.

    Mirrors `app.auth.tokens._lock_family`: the same `hashtextextended`-into-
    `pg_advisory_xact_lock` pattern, so every upload carrying the same client-supplied
    `receipt_id` is fully serialized -- the existence check, the decode, the write and the
    insert all happen while holding it. A second attempt with the same id blocks here until
    the first commits or rolls back, so it never writes to a path the first attempt might
    still be using, and its own existence check (run after the wait) reliably sees the
    first attempt's committed row instead of racing it.
    """
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(receipt_id), 0))))


def _existing_receipt(db: Session, user_id: uuid.UUID, receipt_id: uuid.UUID) -> Receipt | None:
    """The caller's own row for `receipt_id`, if an upload already created one.

    Raises `Conflict` if the id belongs to another user's receipt.
    """
    existing = db.get(Receipt, receipt_id)
    if existing is None:
        return None
    if existing.user_id != user_id:
        raise Conflict("This id belongs to another receipt.")
    return existing


def _store(
    settings: Settings, now: datetime, file_id: uuid.UUID, jpeg_bytes: bytes
) -> tuple[str, Path]:
    """Write `jpeg_bytes` under `receipts_dir/YYYY/MM/<file_id>.jpg`. Returns the path
    relative to `receipts_dir` (what gets stored in the row) and the absolute path.
    """
    relative_path = f"{now.strftime('%Y')}/{now.strftime('%m')}/{file_id}.jpg"
    final_path = settings.receipts_dir / relative_path
    _write_atomically(final_path, jpeg_bytes)
    return relative_path, final_path


def _insert_or_discard(
    db: Session, user_id: uuid.UUID, values: dict[str, object], final_path: Path
) -> tuple[Receipt, bool]:
    """Insert the receipt row, or discard `final_path` if it turns out not to be needed.

    `create_idempotent`'s `INSERT ... ON CONFLICT DO NOTHING` is itself race-safe, but with
    `_lock_receipt_id` held for the whole call this only returns `created=False` in the
    corner cases that lock doesn't cover (`receipt_id=None`, so every caller mints its own
    unique file_id and this never fires) -- kept as a safety net rather than a path this
    code expects to take. The same cleanup covers a failed insert.
    """
    try:
        row, created = create_idempotent(db, Receipt, user_id, values)
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    if not created:
        final_path.unlink(missing_ok=True)
    return row, created


def upload_receipt(
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    transaction_id: uuid.UUID,
    data: bytes,
    receipt_id: uuid.UUID | None,
    now: datetime,
) -> tuple[Receipt, bool]:
    """Re-encode and store `data` as the receipt for `transaction_id`.

    `receipt_id` makes this idempotent for the phone's outbox: a repeat with the same id
    waits on `_lock_receipt_id`, then returns the existing row via a cheap lookup, before
    any image work or file write, so a retried upload never writes a second file and two
    concurrent uploads for the same id can never write to the same path at once.

    The DB insert happens inside this call, but the router commits it afterwards; a commit
    that then fails would leave the file this call wrote with no committed row. That needs
    a connection or server failure between a successful `INSERT ... RETURNING` and `COMMIT`
    on a table with no deferred constraints -- the same narrow, already-accepted risk every
    other write in this app carries, not one specific to receipts, and the nightly backup
    (`pg_dump` plus the receipts folder together) is where that class of failure gets
    noticed and recovered from, not a per-request guard.
    """
    get_owned(db, Transaction, transaction_id, user_id)

    if receipt_id is not None:
        _lock_receipt_id(db, receipt_id)
        existing = _existing_receipt(db, user_id, receipt_id)
        if existing is not None:
            return existing, False

    if len(data) > MAX_UPLOAD_BYTES:
        raise PayloadTooLarge()

    jpeg_bytes = _reencode(data)
    sha256 = hashlib.sha256(jpeg_bytes).hexdigest()

    file_id = receipt_id or uuid.uuid4()
    relative_path, final_path = _store(settings, now, file_id, jpeg_bytes)

    values = {
        "id": file_id,
        "transaction_id": transaction_id,
        "file_path": relative_path,
        "mime": "image/jpeg",
        "sha256": sha256,
    }
    return _insert_or_discard(db, user_id, values, final_path)


def resolve_receipt_file(
    db: Session, settings: Settings, user_id: uuid.UUID, id: uuid.UUID
) -> Path:
    """The on-disk path for receipt `id`, scoped to `user_id`.

    Raises `NotFound` for a missing/other-user row, for a `file_path` that resolves
    outside `receipts_dir` (a tampered row), and for a path that resolves inside it but
    isn't actually there; the last two are logged, since they mean something is wrong on
    disk or in the database rather than a client simply guessing ids.
    """
    receipt = get_owned(db, Receipt, id, user_id)
    base = settings.receipts_dir.resolve()
    full = (base / receipt.file_path).resolve()
    if not full.is_relative_to(base):
        logger.error("Receipt {} file_path escapes receipts_dir: {}", receipt.id, receipt.file_path)
        raise NotFound()
    if not full.is_file():
        logger.error("Receipt {} has no file on disk at {}", receipt.id, full)
        raise NotFound()
    return full
