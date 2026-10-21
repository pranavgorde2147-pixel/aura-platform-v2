"""User API routes for user management."""

from __future__ import annotations

import hmac
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserResponse
from app.services.user import DuplicateEmailError, UserCreationError, UserService

logger = get_logger(__name__)
router = APIRouter()


def _registration_allowed(bootstrap_token: str | None) -> bool:
    """Return whether account creation is permitted for this request.

    When ``ALLOW_PUBLIC_REGISTRATION`` is enabled (the default) anyone may
    register. When it is disabled, only a request that presents the configured
    ``BOOTSTRAP_TOKEN`` may create the initial account(s); this closes public
    sign-up without needing an admin role that does not exist yet.
    """
    settings = get_settings()
    if settings.allow_public_registration:
        return True
    expected = settings.bootstrap_token
    if not expected or not bootstrap_token:
        return False
    return hmac.compare_digest(bootstrap_token, expected)


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Users"],
)
async def create_user(
    data: UserCreate,
    session: AsyncSession = Depends(get_db),
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
) -> UserResponse:
    """Create a new user account with a default identity and Personal Brain."""
    if not _registration_allowed(x_bootstrap_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled",
        )

    service = UserService(session)

    if await service.user_email_exists(data.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    try:
        user = await service.create_user(
            email=data.email,
            password_hash=hash_password(data.password),
            full_name=data.full_name,
        )
    except DuplicateEmailError as exc:
        # Race between the pre-check and the insert.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        ) from exc
    except UserCreationError as exc:
        # Details are logged server-side by the service; clients get a safe message.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create account",
        ) from exc

    logger.info("Registered new user account %s", user.id)
    return user


@router.get(
    "/users/{user_id}",
    response_model=UserResponse,
    tags=["Users"],
)
async def get_user(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve the authenticated user's profile by ID."""
    if current_user.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's record")

    service = UserService(session)
    user = await service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


@router.get(
    "/users/email/{email}",
    response_model=UserResponse,
    tags=["Users"],
)
async def get_user_by_email(
    email: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve the authenticated user by email address."""
    if current_user.email.lower() != email.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's account")

    service = UserService(session)
    user = await service.get_user_by_email(email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user
