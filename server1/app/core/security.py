"""Password hashing and security helpers for AURA Server 1."""

from __future__ import annotations

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