"""User API routes for user management."""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import get_db
from app.services.user import UserService
from app.schemas.user import UserCreate, UserResponse
from app.core.logging import get_logger

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
    
    # Check if email already exists
    if await service.user_email_exists(data.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
    # TODO: Hash password properly using security module
    user = await service.create_user(
        email=data.email,
        password_hash=data.password,  # TODO: Use bcrypt
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
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve a user by ID."""
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
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve a user by email address."""
    service = UserService(session)
    user = await service.get_user_by_email(email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user
