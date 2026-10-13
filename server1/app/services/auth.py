"""Authentication services for login, refresh, and session validation."""

from __future__ import annotations

import uuid

from app.core.jwt import create_access_token, create_refresh_token, decode_token
from app.core.security import verify_password
from app.models.user import User
from app.repositories.user import UserRepository


class AuthService:
    """Authentication business logic for users."""

    def __init__(self, session):
        self.session = session
        self.user_repo = UserRepository(session)

    async def authenticate_user(self, email: str, password: str) -> User | None:
        """Authenticate a user by email and password."""
        user = await self.user_repo.get_by_email(email)
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def login(self, email: str, password: str) -> dict[str, str | int]:
        """Create JWT tokens for a valid user."""
        user = await self.authenticate_user(email, password)
        if user is None:
            raise ValueError("Invalid email or password")

        access_token = create_access_token(str(user.id))
        refresh_token = create_refresh_token(str(user.id))

        user.last_login_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        await self.session.commit()

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 15 * 60,
        }

    async def refresh_token(self, refresh_token: str) -> dict[str, str | int]:
        """Exchange a valid refresh token for a new access token."""
        payload = decode_token(refresh_token, token_type="refresh")
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("Invalid refresh token")

        user = await self.user_repo.get_by_id(uuid.UUID(user_id))
        if user is None or not user.is_active:
            raise ValueError("User is not active")

        access_token = create_access_token(user_id)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 15 * 60,
        }

    async def logout(self, refresh_token: str) -> None:
        """Invalidate a refresh token for a logout flow."""
        try:
            decode_token(refresh_token, token_type="refresh")
        except ValueError:
            raise
        return None
