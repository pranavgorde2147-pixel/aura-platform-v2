"""Connected applications and consent for one Brain.

Server 1 owns the app catalog and the user's consent decisions. Server 2 keeps
a *Brain-scoped authorization snapshot* of those decisions so that vault reads,
writes, links and cross-app routing can be authorized at the storage boundary
without a synchronous call back to Server 1.

The snapshot is written only through this service, and only by an
authenticated Server 1 call. A Brain without a matching connection can never
read or write that app's data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import BrainAppConnection
from app.storage_db import ManagedBrain

#: Consent states a connection can be in.
CONSENT_GRANTED = "granted"
CONSENT_REVOKED = "revoked"
CONSENT_SUSPENDED = "suspended"


class BrainAppAuthorizationError(PermissionError):
    """Raised when an app is not authorized for the requested Brain action."""


class BrainAppConnectionService:
    """Manage and enforce the connected-app snapshot of a Brain."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- writes ------------------------------------------------------------
    async def set_connection(
        self,
        brain: ManagedBrain,
        *,
        app_slug: str,
        granted_scopes: list[dict[str, str]],
        is_active: bool = True,
        consent_state: str = CONSENT_GRANTED,
    ) -> BrainAppConnection:
        """Create or replace the authorization snapshot of one app."""
        if not app_slug or "/" in app_slug:
            raise ValueError("app_slug must be a simple slug")
        result = await self.session.execute(
            select(BrainAppConnection).where(
                BrainAppConnection.brain_id == brain.brain_id,
                BrainAppConnection.app_slug == app_slug,
            )
        )
        connection = result.scalars().first()
        normalized = [
            {"scope": str(item.get("scope", "")), "action": str(item.get("action", ""))}
            for item in granted_scopes
            if isinstance(item, dict) and item.get("scope")
        ]
        if connection is None:
            connection = BrainAppConnection(
                brain_id=brain.brain_id,
                owner_id=brain.owner_id,
                app_slug=app_slug,
                is_active=is_active,
                granted_scopes=normalized,
                consent_state=consent_state,
                consent_recorded_at=datetime.now(timezone.utc),
            )
            self.session.add(connection)
        else:
            connection.granted_scopes = normalized
            connection.is_active = is_active
            connection.consent_state = consent_state
            connection.consent_recorded_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.session.refresh(connection)
        await self._sync_count(brain)
        return connection

    async def revoke_connection(self, brain: ManagedBrain, app_slug: str) -> bool:
        """Remove the authorization snapshot of one app."""
        result = await self.session.execute(
            select(BrainAppConnection).where(
                BrainAppConnection.brain_id == brain.brain_id,
                BrainAppConnection.app_slug == app_slug,
            )
        )
        connection = result.scalars().first()
        if connection is None:
            return False
        await self.session.delete(connection)
        await self.session.commit()
        await self._sync_count(brain)
        return True

    async def _sync_count(self, brain: ManagedBrain) -> None:
        """Keep ``connected_app_count`` in the registry in step."""
        count = int(
            await self.session.scalar(
                select(func.count(BrainAppConnection.id)).where(
                    BrainAppConnection.brain_id == brain.brain_id,
                    BrainAppConnection.is_active.is_(True),
                )
            )
            or 0
        )
        brain.connected_app_count = count
        await self.session.commit()

    # -- reads -------------------------------------------------------------
    async def list_connections(self, brain: ManagedBrain) -> list[BrainAppConnection]:
        result = await self.session.execute(
            select(BrainAppConnection)
            .where(BrainAppConnection.brain_id == brain.brain_id)
            .order_by(BrainAppConnection.app_slug)
        )
        return list(result.scalars().all())

    async def get_connection(
        self, brain: ManagedBrain, app_slug: str
    ) -> BrainAppConnection | None:
        result = await self.session.execute(
            select(BrainAppConnection).where(
                BrainAppConnection.brain_id == brain.brain_id,
                BrainAppConnection.app_slug == app_slug,
            )
        )
        return result.scalars().first()

    async def has_permission(
        self,
        brain: ManagedBrain,
        app_slug: str,
        scope: str,
        action: str,
    ) -> bool:
        """Return True when the app holds ``scope:action`` for this Brain."""
        connection = await self.get_connection(brain, app_slug)
        if connection is None or not connection.is_active:
            return False
        if connection.consent_state != CONSENT_GRANTED:
            return False
        return any(
            grant.get("scope") == scope and grant.get("action") == action
            for grant in (connection.granted_scopes or [])
        )

    async def require_permission(
        self,
        brain: ManagedBrain,
        app_slug: str,
        scope: str,
        action: str,
    ) -> BrainAppConnection:
        """Raise unless the app holds ``scope:action`` for this Brain."""
        if not await self.has_permission(brain, app_slug, scope, action):
            raise BrainAppAuthorizationError(
                f"App '{app_slug}' is not authorized for {scope}:{action} on this Brain"
            )
        connection = await self.get_connection(brain, app_slug)
        assert connection is not None  # noqa: S101 - guarded by has_permission
        return connection

    async def connected_app_count(self, brain: ManagedBrain) -> int:
        return int(
            await self.session.scalar(
                select(func.count(BrainAppConnection.id)).where(
                    BrainAppConnection.brain_id == brain.brain_id,
                    BrainAppConnection.is_active.is_(True),
                )
            )
            or 0
        )


def connection_metadata(connection: BrainAppConnection) -> dict[str, Any]:
    """Return the transport representation of one connection."""
    return {
        "app_slug": connection.app_slug,
        "is_active": connection.is_active,
        "consent_state": connection.consent_state,
        "granted_scopes": list(connection.granted_scopes or []),
        "consent_recorded_at": connection.consent_recorded_at.isoformat()
        if connection.consent_recorded_at
        else None,
        "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
    }


__all__ = [
    "BrainAppAuthorizationError",
    "BrainAppConnectionService",
    "CONSENT_GRANTED",
    "CONSENT_REVOKED",
    "CONSENT_SUSPENDED",
    "connection_metadata",
]
