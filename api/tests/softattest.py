"""A software App Attest device for tests: real P-256 keys and real X.509 certificates.

`FakeAppleCA` stands in for Apple's App Attestation CA (a root and an intermediate), and
`SoftAppAttest` produces attestation and assertion objects the way DCAppAttestService does,
so they go through the server's actual chain, nonce and signature checks.
"""

import base64
import hashlib
import struct
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509.oid import NameOID
from webauthn.helpers.encode_cbor import encode_cbor

NONCE_OID = x509.ObjectIdentifier("1.2.840.113635.100.8.2")
FLAG_AT = 0x40
AAGUID_PRODUCTION = b"appattest" + bytes(7)
AAGUID_DEVELOPMENT = b"appattestdevelop"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    subject: str,
    public_key: ec.EllipticCurvePublicKey,
    issuer: str,
    signing_key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
    nonce: bytes | None = None,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    if nonce is not None:
        # SEQUENCE { [1] EXPLICIT OCTET STRING nonce }, as Apple encodes it.
        value = bytes([0x30, 0x24, 0xA1, 0x22, 0x04, 0x20]) + nonce
        builder = builder.add_extension(x509.UnrecognizedExtension(NONCE_OID, value), False)
    return builder.sign(signing_key, hashes.SHA256())


class FakeAppleCA:
    """A root and intermediate CA in the shape of Apple's App Attestation CA."""

    def __init__(self) -> None:
        self.root_key = ec.generate_private_key(ec.SECP384R1())
        self.root = _certificate(
            "Fake App Attestation Root CA",
            self.root_key.public_key(),
            "Fake App Attestation Root CA",
            self.root_key,
            ca=True,
        )
        self.intermediate_key = ec.generate_private_key(ec.SECP384R1())
        self.intermediate = _certificate(
            "Fake App Attestation CA 1",
            self.intermediate_key.public_key(),
            "Fake App Attestation Root CA",
            self.root_key,
            ca=True,
        )

    @property
    def root_pem(self) -> bytes:
        return self.root.public_bytes(Encoding.PEM)

    def x5c(self, public_key: ec.EllipticCurvePublicKey, nonce: bytes) -> list[bytes]:
        """The credential certificate for `public_key` carrying `nonce`, then the intermediate."""
        leaf = _certificate(
            "fake-credential",
            public_key,
            "Fake App Attestation CA 1",
            self.intermediate_key,
            ca=False,
            nonce=nonce,
        )
        return [leaf.public_bytes(Encoding.DER), self.intermediate.public_bytes(Encoding.DER)]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class SoftAppAttest:
    """One device's attested key, answering challenges like DCAppAttestService."""

    def __init__(self, ca: FakeAppleCA, app_id: str, aaguid: bytes = AAGUID_PRODUCTION) -> None:
        self.ca = ca
        self.app_id = app_id
        self.aaguid = aaguid
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.counter = 0

    @property
    def public_point(self) -> bytes:
        return self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

    @property
    def key_id_bytes(self) -> bytes:
        return hashlib.sha256(self.public_point).digest()

    @property
    def key_id(self) -> str:
        return b64(self.key_id_bytes)

    def _rp_id_hash(self, app_id: str | None) -> bytes:
        return hashlib.sha256((app_id or self.app_id).encode()).digest()

    def attest(
        self,
        challenge: bytes,
        *,
        nonce_challenge: bytes | None = None,
        app_id: str | None = None,
        counter: int = 0,
        aaguid: bytes | None = None,
        ca: FakeAppleCA | None = None,
    ) -> str:
        """A base64 attestation object for `challenge`. The keyword arguments break one check
        each: `nonce_challenge` builds the nonce from another challenge, `ca` issues the
        certificates from an untrusted CA."""
        key_id = self.key_id_bytes
        auth_data = (
            self._rp_id_hash(app_id)
            + bytes([FLAG_AT])
            + struct.pack(">I", counter)
            + (aaguid or self.aaguid)
            + struct.pack(">H", len(key_id))
            + key_id
        )
        client_data_hash = hashlib.sha256(nonce_challenge or challenge).digest()
        nonce = hashlib.sha256(auth_data + client_data_hash).digest()
        x5c = (ca or self.ca).x5c(self.key.public_key(), nonce)
        return b64(
            encode_cbor(
                {
                    "fmt": "apple-appattest",
                    "attStmt": {"x5c": x5c, "receipt": b"fake-receipt"},
                    "authData": auth_data,
                }
            )
        )

    def assertion(
        self,
        body: bytes,
        challenge: bytes,
        *,
        counter_step: int = 1,
        app_id: str | None = None,
        signing_key: ec.EllipticCurvePrivateKey | None = None,
    ) -> str:
        """A base64 assertion over clientData = `body` || `challenge`."""
        self.counter += counter_step
        auth_data = self._rp_id_hash(app_id) + bytes([0]) + struct.pack(">I", self.counter)
        client_data_hash = hashlib.sha256(body + challenge).digest()
        nonce = hashlib.sha256(auth_data + client_data_hash).digest()
        signature = (signing_key or self.key).sign(nonce, ec.ECDSA(hashes.SHA256()))
        return b64(encode_cbor({"signature": signature, "authenticatorData": auth_data}))
