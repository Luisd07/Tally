import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import Bucket, MatchField, MatchType


class Rule(UUIDPk, Timestamps, Base):
    __tablename__ = "rules"
    # No "at least one set_* field" CHECK here on purpose (#51 UB-2/MV-6): it would
    # conflict with `set_category_id`'s own `ON DELETE SET NULL` the moment a rule's
    # only `set_*` field was a category that then got deleted, turning a routine
    # delete into an IntegrityError. The requirement is enforced at the schema level
    # on create and on the merged row on PATCH (`app.schemas.rules`,
    # `app.services.rules.update_rule`); the engine itself skips a stored rule that
    # has since lost every `set_*` field (`app.services.rules._first_match`).
    __table_args__ = (Index("ix_rules_user_id_priority", "user_id", "priority"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Lower runs first; ties broken by created_at then id (app.services.rules.apply_rules).
    priority: Mapped[int] = mapped_column(Integer)
    match_field: Mapped[MatchField] = mapped_column(Enum(MatchField, native_enum=False, length=16))
    # No regex: a client-supplied pattern compiled as regex and run against every new
    # transaction would be a ReDoS vector on an endpoint anyone holding a token can write
    # to. `contains`/`equals`/`starts_with` cover the spec's own example ("always file
    # HERTZ as...") without that risk.
    match_type: Mapped[MatchType] = mapped_column(Enum(MatchType, native_enum=False, length=16))
    pattern: Mapped[str] = mapped_column(String(200))
    set_merchant: Mapped[str | None] = mapped_column(String(200))
    set_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    set_bucket: Mapped[Bucket | None] = mapped_column(Enum(Bucket, native_enum=False, length=16))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
