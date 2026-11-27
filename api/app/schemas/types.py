"""Shared constrained scalar types for request/response schemas.

Each alias mirrors a DB CHECK or column length, defined once so Create and Update
schemas can't drift apart from each other.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

Name100 = Annotated[str, Field(min_length=1, max_length=100)]
Text100 = Annotated[str, Field(max_length=100)]
Text50 = Annotated[str, Field(max_length=50)]
Text200 = Annotated[str, Field(max_length=200)]
# rules.pattern: String(200) NOT NULL, min 1 -- an empty pattern would match everything.
# `min_length=1` alone still lets a whitespace-only string through; the normalized form
# (collapsed and uppercased) is rejected by a validator in app.schemas.rules.
Pattern200 = Annotated[str, Field(min_length=1, max_length=200)]
Text500 = Annotated[str, Field(max_length=500)]
Text2000 = Annotated[str, Field(max_length=2000)]
Text2048 = Annotated[str, Field(max_length=2048)]
Last4 = Annotated[str, Field(pattern=r"^[0-9]{4}$")]
DayOfMonth = Annotated[int, Field(ge=1, le=31)]
# rules.priority is a plain Postgres INTEGER; bound it so an out-of-range value is a 422
# from pydantic rather than a DataError-turned-500 from the DB.
Priority = Annotated[int, Field(ge=0, le=2**31 - 1)]
Money = Annotated[Decimal, Field(max_digits=12, decimal_places=2)]
MoneyNonNegative = Annotated[Decimal, Field(max_digits=12, decimal_places=2, ge=0)]
# subscriptions.amount is CHECK < 0 at the DB: SimpleFIN's sign for an outflow, never a
# positive bill amount. Rejecting the sign here keeps a bad value a 422, not a DB 500.
NegativeMoney = Annotated[Decimal, Field(max_digits=12, decimal_places=2, lt=0)]
Percent = Annotated[int, Field(ge=0, le=100)]
Apr = Annotated[Decimal, Field(max_digits=5, decimal_places=2, ge=0, le=100)]
ApnsToken = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[0-9a-fA-F]+$")]
