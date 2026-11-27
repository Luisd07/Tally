"""A software passkey for tests: a real P-256 key producing real WebAuthn responses.

Responses go through py_webauthn's actual verification, so the tests prove the crypto path.
"""

import hashlib
import json
import os
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.encode_cbor import encode_cbor

FLAG_UP = 0x01  # user present
FLAG_UV = 0x04  # user verified
FLAG_AT = 0x40  # attested credential data included
COSE_ES256 = -7


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0

    @property
    def cose_public_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return encode_cbor(
            {
                1: 2,  # kty: EC2
                3: COSE_ES256,  # alg
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin or self.origin}
        ).encode()

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        rp_id_hash = hashlib.sha256(self.rp_id.encode()).digest()
        return rp_id_hash + bytes([flags]) + struct.pack(">I", self.sign_count) + attested

    def register(
        self,
        options: dict[str, Any],
        *,
        challenge: str | None = None,
        origin: str | None = None,
        flags: int = FLAG_UP | FLAG_UV | FLAG_AT,
    ) -> dict[str, Any]:
        """Answer registration options with a `none`-attestation RegistrationResponseJSON."""
        client_data = self._client_data(
            "webauthn.create", challenge or options["challenge"], origin
        )
        attested = (
            bytes(16)  # AAGUID: none attestation reports zeros
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self.cose_public_key
        )
        attestation_object = encode_cbor(
            {"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, attested)}
        )
        credential_id = bytes_to_base64url(self.credential_id)
        return {
            "id": credential_id,
            "rawId": credential_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation_object),
                "transports": ["internal"],
            },
        }

    def sign_in(
        self,
        options: dict[str, Any],
        *,
        user_handle: bytes | None,
        challenge: str | None = None,
        origin: str | None = None,
        flags: int = FLAG_UP | FLAG_UV,
        counter_step: int = 1,
        signing_key: ec.EllipticCurvePrivateKey | None = None,
    ) -> dict[str, Any]:
        """Answer authentication options with a signed AuthenticationResponseJSON.

        `counter_step=0` behaves like iCloud Keychain passkeys, which always report 0.
        """
        self.sign_count += counter_step
        client_data = self._client_data("webauthn.get", challenge or options["challenge"], origin)
        auth_data = self._auth_data(flags)
        signature = (signing_key or self.key).sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        credential_id = bytes_to_base64url(self.credential_id)
        response = {
            "clientDataJSON": bytes_to_base64url(client_data),
            "authenticatorData": bytes_to_base64url(auth_data),
            "signature": bytes_to_base64url(signature),
        }
        if user_handle is not None:
            response["userHandle"] = bytes_to_base64url(user_handle)
        return {
            "id": credential_id,
            "rawId": credential_id,
            "type": "public-key",
            "response": response,
        }
