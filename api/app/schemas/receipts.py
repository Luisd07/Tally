import uuid
from datetime import date, datetime
from decimal import Decimal

from app.models.enums import ReceiptReadingStatus
from app.schemas.base import Schema


class LineItemOut(Schema):
    description: str
    amount: Decimal


class ReceiptReadingOut(Schema):
    """What Claude read off the receipt (#170), for the user to confirm. `adds_up` is false
    when the line items, tax and tip don't make the total; `matches_transaction` is false when
    the total isn't the charge. Either is null when there was nothing to check."""

    status: ReceiptReadingStatus
    merchant: str | None
    receipt_date: date | None
    subtotal: Decimal | None
    tax: Decimal | None
    tip: Decimal | None
    total: Decimal | None
    line_items: list[LineItemOut]
    category_id: uuid.UUID | None
    adds_up: bool | None
    matches_transaction: bool | None


class ReceiptOut(Schema):
    """Never exposes `file_path`: the client only needs the id to fetch the image back."""

    id: uuid.UUID
    transaction_id: uuid.UUID
    mime: str
    sha256: str
    uploaded_at: datetime


class ReceiptWithReading(ReceiptOut):
    """A receipt and its reading; `reading` is null until the worker has read it."""

    reading: ReceiptReadingOut | None
