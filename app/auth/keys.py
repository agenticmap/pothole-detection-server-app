"""RS256 signing-key management + JWKS publication for the staff-auth tier.

Asymmetric (RS256) on purpose: the private key signs access tokens, the public
key is published at /.well-known/jwks.json. Relying parties validate by `kid`
against the JWKS, so the token ISSUER can later be swapped for OIDC/SSO without
changing the API's validation path (Phase 2 of the staged auth rollout).

Key source (in order):
  1. settings.auth_jwt_private_key_pem (PEM; literal "\\n" escapes are accepted).
  2. If empty AND env == "development": an ephemeral keypair is generated at
     startup. Tokens won't survive a restart — fine for local dev only.
  3. If empty in any other env: hard error (fail closed).
"""

import base64
import logging
from dataclasses import dataclass
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeyMaterial:
    """The active signing keypair (PEM bytes) and its key id."""

    private_pem: bytes
    public_pem: bytes
    kid: str


def _normalize_pem(raw: str) -> bytes:
    """Accept PEM either with real newlines or with literal '\\n' escapes (env-friendly)."""
    return raw.strip().replace("\\n", "\n").encode("utf-8")


def _public_pem_from_private(private_pem: bytes) -> bytes:
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@lru_cache(maxsize=1)
def get_key_material() -> KeyMaterial:
    """Resolve the active signing keypair once per process."""
    kid = settings.auth_jwt_kid

    if settings.auth_jwt_private_key_pem.strip():
        private_pem = _normalize_pem(settings.auth_jwt_private_key_pem)
        public_pem = (
            _normalize_pem(settings.auth_jwt_public_key_pem)
            if settings.auth_jwt_public_key_pem.strip()
            else _public_pem_from_private(private_pem)
        )
        return KeyMaterial(private_pem=private_pem, public_pem=public_pem, kid=kid)

    if settings.env != "development":
        raise RuntimeError(
            "AUTH_JWT_PRIVATE_KEY_PEM is required outside development "
            "(refusing to mint tokens with an ephemeral key in production)."
        )

    logger.warning(
        "No AUTH_JWT_PRIVATE_KEY_PEM configured — generating an EPHEMERAL RS256 "
        "keypair. Staff tokens will not survive a restart. Dev only."
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return KeyMaterial(private_pem=private_pem, public_pem=public_pem, kid=kid)


def _b64url_uint(value: int) -> str:
    """Encode a non-negative integer as unpadded base64url (JWKS `n`/`e` format)."""
    length = (value.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def get_jwks() -> dict:
    """Public JWKS for /.well-known/jwks.json.

    Exposes the current key. During a rotation, a second (previous) key would be
    appended here until its tokens have all expired; consumers select by `kid`.
    """
    km = get_key_material()
    public_key = serialization.load_pem_public_key(km.public_pem)
    numbers = public_key.public_numbers()  # type: ignore[attr-defined]
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": km.kid,
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }
