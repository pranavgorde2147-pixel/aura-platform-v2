"""Authentication services for login, refresh, and session validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.jwt import create_access_token, create_refresh_token, decode_token
from app.core.security import hash_token, tokens_match, verify_password
from app.models.user import User
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository

_ACCESS_TTL_SECONDS = 15 * 60


def _as_aware(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime, assuming UTC for naive values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class AuthService:
    """Authentication business logic for users.

    Refresh tokens are persisted as sessions (hashed) and rotated on every use.
    Presenting an already-rotated token is treated as reuse and revokes all of
    the user's sessions.
    """

    def __init__(self, session):
        self.session = session
        self.user_repo = UserRepository(session)
        self.session_repo = SessionRepository(session)

    async def authenticate_user(self, email: str, password: str) -> User | None:
        """Authenticate a user by email and password."""
        user = await self.user_repo.get_by_email(email)
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def _issue_session(
        self,
        user: User,
        *,
        user_agent: str | None,
        ip_address: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        """Create a persisted session and return ``(access_token, refresh_token)``."""
        settings = get_settings()
        session_id = uuid.uuid4()
        refresh_token = create_refresh_token(str(user.id), jti=str(session_id))
        await self.session_repo.create(
            id=session_id,
            user_id=user.id,
            refresh_token_hash=hash_token(refresh_token),
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
            is_revoked=False,
            user_agent=user_agent,
            ip_address=ip_address,
            last_used_at=now,
        )
        access_token = create_access_token(str(user.id))
        return access_token, refresh_token

    async def login(
        self,
        email: str,
        password: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> dict[str, str | int]:
        """Create JWT tokens for a valid user and persist a session."""
        user = await self.authenticate_user(email, password)
        if user is None:
            raise ValueError("Invalid email or password")

        now = datetime.now(timezone.utc)
        access_token, refresh_token = await self._issue_session(
            user, user_agent=user_agent, ip_address=ip_address, now=now
        )

        user.last_login_at = now
        await self.session.commit()

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": _ACCESS_TTL_SECONDS,
        }

    async def refresh_token(
        self,
        refresh_token: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> dict[str, str | int]:
        """Rotate a valid refresh token into a new session and access token.

        Raises ``ValueError`` for invalid, expired, revoked, or reused tokens.
        """
        try:
            payload = decode_token(refresh_token, token_type="refresh")
        except ValueError as exc:
            raise ValueError("Invalid refresh token") from exc

        user_id = payload.get("sub")
        jti = payload.get("jti")
        if not user_id or not jti:
            raise ValueError("Invalid refresh token")

        try:
            session_id = uuid.UUID(str(jti))
        except ValueError as exc:
            raise ValueError("Invalid refresh token") from exc

        session = await self.session_repo.get_by_id(session_id)
        if session is None:
            raise ValueError("Invalid refresh token")

        now = datetime.now(timezone.utc)

        # Reuse detection: a rotated/revoked token being presented again means it
        # leaked. Revoke every session for the user as a precaution.
        if session.is_revoked:
            await self.session_repo.revoke_all_for_user(session.user_id)
            await self.session.commit()
            raise ValueError("Refresh token has been revoked")

        if _as_aware(session.expires_at) <= now:
            session.is_revoked = True
            session.revoked_at = now
            await self.session.commit()
            raise ValueError("Refresh token has expired")

        if not tokens_match(refresh_token, session.refresh_token_hash):
            raise ValueError("Invalid refresh token")

        user = await self.user_repo.get_by_id(uuid.UUID(str(user_id)))
        if user is None or not user.is_active:
            raise ValueError("User is not active")

        # Rotate: revoke the presented session and issue a fresh one.
        session.is_revoked = True
        session.revoked_at = now
        session.last_used_at = now
        access_token, new_refresh = await self._issue_session(
            user,
            user_agent=user_agent or session.user_agent,
            ip_address=ip_address or session.ip_address,
            now=now,
        )
        await self.session.commit()

        return {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "token_type": "bearer",
            "expires_in": _ACCESS_TTL_SECONDS,
        }

    async def logout(self, refresh_token: str) -> None:
        """Revoke the session tied to a refresh token (idempotent)."""
        try:
            payload = decode_token(refresh_token, token_type="refresh")
        except ValueError:
            return None

        jti = payload.get("jti")
        if not jti:
            return None
        try:
            session_id = uuid.UUID(str(jti))
        except ValueError:
            return None

        await self.session_repo.revoke(session_id)
        await self.session.commit()
        return None

    async def revoke_all_sessions(self, user_id: uuid.UUID) -> int:
        """Revoke all sessions for a user (used for a forced sign-out)."""
        revoked = await self.session_repo.revoke_all_for_user(user_id)
        await self.session.commit()
        return revoked
