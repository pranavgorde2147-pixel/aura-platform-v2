"""User API routes for user management."""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserResponse
from app.services.user import UserService

logger = get_logger(__name__)
router = APIRouter()


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Users"],
)
async def create_user(
    data: UserCreate,
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Create a new user account."""
    service = UserService(session)

    if await service.user_email_exists(data.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    user = await service.create_user(
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
    )
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
