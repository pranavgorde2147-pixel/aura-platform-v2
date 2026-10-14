"""Authentication API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _client_metadata(request: Request) -> tuple[str | None, str | None]:
    """Return a bounded user-agent and the client IP for session audit fields."""
    user_agent = request.headers.get("user-agent")
    if user_agent is not None:
        user_agent = user_agent[:500]
    client_ip = request.client.host if request.client else None
    return user_agent, client_ip


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate a user and return JWT access and refresh tokens."""
    user_agent, client_ip = _client_metadata(request)
    service = AuthService(session)
    try:
        token_payload = await service.login(
            payload.email, payload.password, user_agent=user_agent, ip_address=client_ip
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return TokenResponse(**token_payload)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Refresh an access token using a valid refresh token."""
    user_agent, client_ip = _client_metadata(request)
    service = AuthService(session)
    try:
        token_payload = await service.refresh_token(
            payload.refresh_token, user_agent=user_agent, ip_address=client_ip
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return TokenResponse(**token_payload)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Invalidate the session tied to a refresh token (idempotent)."""
    del current_user
    service = AuthService(session)
    await service.logout(payload.refresh_token)
    return None


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)) -> dict[str, str]:
    """Return the currently authenticated user."""
    return {"user_id": str(current_user.id), "email": current_user.email}
