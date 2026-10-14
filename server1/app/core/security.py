"""Password hashing and security helpers for AURA Server 1."""

from __future__ import annotations

import hashlib
import hmac

import bcrypt

_BCRYPT_MAX_BYTES = 72


def _truncate(password: str) -> bytes:
    """bcrypt only consumes the first 72 bytes of input; truncate consistently."""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(_truncate(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def hash_token(token: str) -> str:
    """Return a deterministic, non-reversible hash of an opaque token.

    Refresh tokens are high-entropy values, so a fast cryptographic digest is
    sufficient (and avoids the cost of bcrypt on every refresh). The stored
    value can be compared against a presented token without keeping the token
    itself in the database.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(presented: str, stored_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    return hmac.compare_digest(hash_token(presented), stored_hash)