"""Session repository for database access to authenticated sessions.

Refresh tokens are bound to a ``sessions`` row via the token's ``jti`` claim.
Only a SHA-256 hash of the token is stored, so a database leak does not expose
usable refresh tokens.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session
from app.repositories.base import BaseRepository


class SessionRepository(BaseRepository[Session]):
    """Repository for Session model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize SessionRepository with async session."""
        super().__init__(session, Session)

    async def get_by_token_hash(self, refresh_token_hash: str) -> Session | None:
        """Retrieve a session by the hash of its refresh token."""
        result = await self.session.execute(
            select(Session).where(Session.refresh_token_hash == refresh_token_hash)
        )
        return result.scalars().first()

    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]:
        """Return all sessions belonging to a user, newest first."""
        result = await self.session.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke(self, session_id: uuid.UUID) -> bool:
        """Revoke a single session without deleting its audit trail."""
        session = await self.get_by_id(session_id)
        if session is None:
            return False
        if not session.is_revoked:
            session.is_revoked = True
            session.revoked_at = datetime.now(timezone.utc)
            await self.session.flush()
        return True

    async def revoke_all_for_user(
        self, user_id: uuid.UUID, except_session_id: uuid.UUID | None = None
    ) -> int:
        """Revoke every active session for a user (e.g. on token reuse).

        Returns the number of sessions revoked.
        """
        now = datetime.now(timezone.utc)
        statement = (
            update(Session)
            .where(Session.user_id == user_id, Session.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=now)
        )
        if except_session_id is not None:
            statement = statement.where(Session.id != except_session_id)
        result = await self.session.execute(statement)
        await self.session.flush()
        return result.rowcount or 0
