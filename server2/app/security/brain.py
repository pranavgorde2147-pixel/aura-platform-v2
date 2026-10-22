"""Service-side authorization helpers for the Brain APIs.

Server 2 never authenticates end users itself: Server 1 does that and forwards
the authorized owner in ``X-Aura-Owner-Id``. These helpers turn that header
into a verified owner id and enforce the administrative allowlist.
"""

from __future__ import annotations

import os
import uuid

from fastapi import Header, HTTPException, status


def parse_owner_id(value: str | None) -> uuid.UUID:
    """Parse the forwarded owner id, rejecting anything that is not a UUID."""
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Aura-Owner-Id is required for Brain operations",
        )
    try:
        return uuid.UUID(str(value).strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Aura-Owner-Id must be a valid UUID",
        ) from exc


async def require_owner_id(
    x_aura_owner_id: str | None = Header(default=None, alias="X-Aura-Owner-Id"),
) -> uuid.UUID:
    """Return the owner id forwarded by Server 1."""
    return parse_owner_id(x_aura_owner_id)


def admin_owner_ids() -> set[str]:
    """Return the allowlisted administrative owner ids."""
    raw = os.getenv("BRAIN_ADMIN_OWNER_IDS", "")
    allowed: set[str] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            allowed.add(str(uuid.UUID(candidate)))
        except ValueError:
            continue
    return allowed


async def require_brain_admin(
    x_aura_owner_id: str | None = Header(default=None, alias="X-Aura-Owner-Id"),
) -> uuid.UUID:
    """Require an allowlisted administrative owner id."""
    owner_id = parse_owner_id(x_aura_owner_id)
    allowed = admin_owner_ids()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Brain administration is not configured on this service",
        )
    if str(owner_id) not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brain administrator authorization required",
        )
    return owner_id


__all__ = [
    "admin_owner_ids",
    "parse_owner_id",
    "require_brain_admin",
    "require_owner_id",
]
