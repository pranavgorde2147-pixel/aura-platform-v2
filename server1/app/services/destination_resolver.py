"""Destination resolution and authorization for the Brain routing pipeline.

Brain never picks a destination from the raw text. It picks one from the
**application registry** — the same registry that backs ``/api/v1/apps`` — and
then validates that the authenticated user may actually write there.

Every check in this module answers one of the questions the platform must
answer before a record is written:

1. is the destination registered?
2. does it support the required operation (record type)?
3. does it have a working storage integration?
4. does the user have an active connection to it?
5. is the required permission granted?

A missing grant is never bypassed and never granted implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.user import User
from app.schemas.apps import AppDefinition
from app.services.app_registry import (
    AppConnectionService,
    get_catalog_app,
    get_destination_permission,
    get_ingest_permission,
    list_routable_apps,
)


#: Preferred record types per semantic category, most specific first. Used
#: when a destination accepts the category but does not declare the exact
#: record type the understanding layer produced.
CATEGORY_RECORD_TYPE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "lecture": ("lecture_note", "lecture_transcript", "study_material", "note", "document"),
    "study_material": ("study_material", "lecture_note", "concept", "note", "document"),
    "deadline": ("assignment_deadline", "task", "calendar_event", "note"),
    "task": ("task", "action_item", "note"),
    "meeting": ("meeting_summary", "meeting_transcript", "decision", "action_item", "note"),
    "calendar_event": ("calendar_event", "event", "note"),
    "diary_event": ("diary_entry", "personal_event", "note"),
    "health": ("health_record", "wellness_event", "note"),
    "travel": ("trip", "itinerary", "travel_booking", "note"),
    "note": ("note", "document"),
    "document": ("document", "note"),
    "personal_fact": ("memory_fact", "note"),
    "user_preference": ("memory_preference", "note"),
    "project": ("project_note", "note"),
    "unclassified": ("note",),
}


@dataclass(frozen=True)
class DestinationAuthorization:
    """The authorization verdict for one destination."""

    allowed: bool
    reason: str
    required_scope: str | None = None
    required_action: str | None = None

    @property
    def required_permission(self) -> str | None:
        if self.required_scope is None:
            return None
        return f"{self.required_scope}:{self.required_action or 'write'}"


@dataclass
class DestinationCandidate:
    """A registry-declared destination that could receive an item."""

    app: AppDefinition
    record_type: str
    exact_match: bool = True
    alternatives: list[str] = field(default_factory=list)


class DestinationResolver:
    """Resolves and authorizes destination applications for one user."""

    def __init__(self, connections: AppConnectionService) -> None:
        self.connections = connections

    # ------------------------------------------------------------------
    # Source app authorization
    # ------------------------------------------------------------------

    async def authorize_source(self, user: User, slug: str | None) -> DestinationAuthorization:
        """Validate that ``slug`` may submit content into the pipeline."""
        if not slug:
            return DestinationAuthorization(
                allowed=False,
                reason="source_app_required",
            )
        app = get_catalog_app(slug)
        if app is None:
            return DestinationAuthorization(allowed=False, reason="unknown_app")
        permission = get_ingest_permission(slug)
        if permission is None:
            return DestinationAuthorization(
                allowed=False,
                reason="source_app_not_enabled_for_ingest",
            )
        scope, action = permission
        module = await self.connections.get_connection_by_slug(user, slug)
        if module is None:
            return DestinationAuthorization(
                allowed=False,
                reason="connection_missing",
                required_scope=scope,
                required_action=action,
            )
        if not module.is_active:
            return DestinationAuthorization(
                allowed=False,
                reason="connection_inactive",
                required_scope=scope,
                required_action=action,
            )
        if not self.connections.has_permission(module, scope, action):
            return DestinationAuthorization(
                allowed=False,
                reason="permission_missing",
                required_scope=scope,
                required_action=action,
            )
        return DestinationAuthorization(
            allowed=True,
            reason="allowed",
            required_scope=scope,
            required_action=action,
        )

    # ------------------------------------------------------------------
    # Destination candidates
    # ------------------------------------------------------------------

    def candidates_for(
        self,
        category: str,
        record_type: str | None,
        requested: list[str] | None = None,
    ) -> list[DestinationCandidate]:
        """Return the registry destinations that could hold an item.

        The registry declares, per application, which semantic categories it
        accepts and which record types it can store. A category therefore
        never implies a destination on its own: only registered applications
        that declare support are returned.

        When the user explicitly requested destinations, the candidate list is
        restricted to them, and only applications that actually declare the
        category remain eligible.
        """
        requested_slugs = {slug for slug in (requested or []) if slug}
        candidates: list[DestinationCandidate] = []
        for app in list_routable_apps():
            if requested_slugs and app.slug not in requested_slugs:
                continue
            if category not in app.categories:
                continue
            if not app.record_types:
                # Nothing can be stored here, so it is not a candidate.
                continue
            if record_type and record_type in app.record_types:
                candidates.append(
                    DestinationCandidate(app=app, record_type=record_type, exact_match=True)
                )
                continue
            # The application accepts the category but not this exact record
            # type: fall back to the closest declared type and keep the
            # alternative visible to the user.
            fallback = self._preferred_record_type(category, app.record_types)
            alternatives = [rt for rt in app.record_types if rt != fallback]
            candidates.append(
                DestinationCandidate(
                    app=app,
                    record_type=fallback,
                    exact_match=False,
                    alternatives=alternatives,
                )
            )
        return candidates

    @staticmethod
    def _preferred_record_type(category: str, record_types: list[str]) -> str:
        """Pick the record type that best fits a category for one destination.

        The understanding layer may produce a record type that a destination
        does not declare. Rather than rejecting the item, the closest declared
        type is used; the substitution stays visible in the response.
        """
        for preferred in CATEGORY_RECORD_TYPE_PREFERENCE.get(category, ()):
            if preferred in record_types:
                return preferred
        return record_types[0]

    # ------------------------------------------------------------------
    # Destination authorization
    # ------------------------------------------------------------------
    async def authorize_destination(
        self,
        user: User,
        slug: str,
        record_type: str,
    ) -> DestinationAuthorization:
        """Validate that a record may be written into ``slug``."""
        app = get_catalog_app(slug)
        if app is None:
            return DestinationAuthorization(allowed=False, reason="unknown_app")
        if not app.routable:
            return DestinationAuthorization(allowed=False, reason="not_routable")
        if app.storage_adapter == "none" or not app.record_types:
            return DestinationAuthorization(allowed=False, reason="no_storage_integration")
        if record_type not in app.record_types:
            return DestinationAuthorization(allowed=False, reason="unsupported_record_type")

        permission = get_destination_permission(slug)
        if permission is None:
            return DestinationAuthorization(allowed=False, reason="no_write_permission_declared")
        scope, action = permission

        module = await self.connections.get_connection_by_slug(user, slug)
        if module is None:
            return DestinationAuthorization(
                allowed=False,
                reason="connection_missing",
                required_scope=scope,
                required_action=action,
            )
        if not module.is_active:
            return DestinationAuthorization(
                allowed=False,
                reason="connection_inactive",
                required_scope=scope,
                required_action=action,
            )
        if not self.connections.has_permission(module, scope, action):
            return DestinationAuthorization(
                allowed=False,
                reason="permission_missing",
                required_scope=scope,
                required_action=action,
            )
        return DestinationAuthorization(
            allowed=True,
            reason="allowed",
            required_scope=scope,
            required_action=action,
        )
