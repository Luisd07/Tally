import uuid


class CeremonyFailed(Exception):
    """An auth ceremony step (WebAuthn or App Attest) was refused.

    `reason` is for the server log, never the response.
    """

    def __init__(self, reason: str, user_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id
