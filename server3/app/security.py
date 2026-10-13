"""Internal service authentication for Server 3.

Server 3 never authenticates end users directly. Server 1 authenticates users
and forwards an authorized operation together with the owning user id. Server 3
only trusts requests that carry the shared internal service token, compared in
constant time.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


def _extract_token(authorization: str | None, x_internal_token: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if x_internal_token:
        return x_internal_token.strip()
    return None


async def require_internal_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Validate the shared internal service token."""
    settings = get_settings()
    if not settings.internal_api_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service is not configured with an internal token",
        )

    presented = _extract_token(authorization, x_internal_token)
    if not presented or not hmac.compare_digest(presented, settings.internal_api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal service credentials",
        )
