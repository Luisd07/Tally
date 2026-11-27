import uuid

from sqlalchemy import BigInteger, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class Passkey(UUIDPk, Timestamps, Base):
    __tablename__ = "passkeys"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Raw bytes, as py_webauthn returns them; unique because sign-in looks the passkey up by it.
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # WebAuthn's signature counter is an unsigned 32-bit value, too big for INTEGER.
    sign_count: Mapped[int] = mapped_column(BigInteger)
    device_name: Mapped[str | None] = mapped_column(String(100))
