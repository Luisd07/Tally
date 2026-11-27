import re
import uuid
from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from app.models.import_mapping import SignConvention
from app.schemas.base import Schema

# A format without a year is ambiguous (and a DeprecationWarning on Python 3.14).
_DATE_DIRECTIVES = (r"%[Yy]", r"%[mbB]", r"%d")


class ImportMappingIn(Schema):
    date_column: str = Field(min_length=1, max_length=100)
    amount_column: str | None = Field(default=None, min_length=1, max_length=100)
    debit_column: str | None = Field(default=None, min_length=1, max_length=100)
    credit_column: str | None = Field(default=None, min_length=1, max_length=100)
    description_column: str = Field(min_length=1, max_length=100)
    date_format: str = Field(min_length=2, max_length=40)
    sign_convention: SignConvention = SignConvention.negative_is_out
    has_header: bool = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        single = self.amount_column is not None
        pair = self.debit_column is not None and self.credit_column is not None
        if single == pair or (self.debit_column is None) != (self.credit_column is None):
            raise ValueError("set amount_column, or both debit_column and credit_column")
        if not all(re.search(directive, self.date_format) for directive in _DATE_DIRECTIVES):
            raise ValueError("date_format must include year, month and day")
        try:
            datetime.strptime(datetime(2026, 1, 2).strftime(self.date_format), self.date_format)
        except ValueError as exc:
            raise ValueError(f"date_format is not a usable strptime format: {exc}") from exc
        if not self.has_header:
            columns = [
                self.date_column,
                self.description_column,
                *(c for c in (self.amount_column, self.debit_column, self.credit_column) if c),
            ]
            if not all(c.isascii() and c.isdigit() for c in columns):
                raise ValueError("without a header, columns must be 0-based indexes like '0'")
        return self


class ImportMappingOut(ImportMappingIn):
    id: uuid.UUID
    account_id: uuid.UUID


class ImportRowError(Schema):
    row: int
    message: str


class ImportResult(Schema):
    inserted: int
    skipped_duplicate: int
    skipped_synced_range: int
    errors: list[ImportRowError]
