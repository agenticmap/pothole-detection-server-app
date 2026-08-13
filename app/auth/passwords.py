"""bcrypt password hashing for staff accounts."""

import bcrypt

# bcrypt silently truncates at 72 bytes; guard so over-long inputs aren't
# accepted as equivalent to their 72-byte prefix.
_MAX_BYTES = 72


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash (utf-8 string) of the given password."""
    pw = plaintext.encode("utf-8")[:_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Constant-time compare of a password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plaintext.encode("utf-8")[:_MAX_BYTES],
            password_hash.encode("utf-8"),
        )
    except ValueError:
        # Malformed/empty stored hash → treat as no-match rather than crashing.
        return False
