"""AURA application registry and per-user app connection service.

The catalog below is the single source of truth for which applications and
modules exist (or are planned) in the AURA ecosystem. User connections reuse
the existing InstalledModule / ModulePermission models; this service never
talks to app databases directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.installed_module import InstalledModule, PlatformType
from app.models.module_permission import ModulePermission, PermissionAction
from app.models.user import User
from app.schemas.apps import AppDefinition, ModulePermissionSpec


class AppRegistryError(Exception):
    """Base error for registry operations."""


class UnknownAppError(AppRegistryError):
    """The requested module slug is not in the catalog."""


class AppAlreadyInstalledError(AppRegistryError):
    """The user already has this app installed/authorized."""


class AppPermissionNotAllowedError(AppRegistryError):
    """A requested permission is not declared for the app in the registry."""


CATALOG: dict[str, AppDefinition] = {
    "aura-core": AppDefinition(
        slug="aura-core",
        name="AURA Core",
        description="Central AURA Android assistant for authenticated chat with AURA Brain.",
        client_type="mobile",
        status="live",
        platform_type="aura",
        purpose="Central assistant chat and ecosystem entry point using the user's authorized Brain.",
        sends_to_aura=["chat queries"],
        needs_from_aura=["brain responses"],
        queries_brain=True,
        repo_path="server1/",
        default_permissions=[ModulePermissionSpec(scope="brain", action="read")],
        additional_permissions=[ModulePermissionSpec(scope="identity", action="read")],
        # Chat is query-only; the app gets no memory-write or routed-record access.
        ingest_scope=None,
        routable=False,
    ),
    "brain": AppDefinition(
        slug="brain",
        name="Personal AI Brain",
        description="Primary way to query the user's brain and receive personalized answers.",
        client_type="web",
        status="live",
        platform_type="aura",
        purpose="Query the user's memory, retrieve context, generate personalized responses.",
        sends_to_aura=["queries", "feedback"],
        needs_from_aura=["brain context", "memories", "answers"],
        queries_brain=True,
        ingests=True,
        repo_path="server1/app/api/v1/brain.py",
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        routable=False,
    ),
    "meeting": AppDefinition(
        slug="meeting",
        name="AURA Meeting",
        description="Android meeting app: camera, transcripts, notes, decisions and action items.",
        client_type="mobile",
        status="live",
        platform_type="aura",
        purpose="Capture meeting content and deliver transcript/summary, decisions and follow-ups to the brain.",
        sends_to_aura=["meeting transcripts", "summaries", "decisions", "action items"],
        needs_from_aura=["brain context", "memory retrieval", "notifications"],
        queries_brain=True,
        ingests=True,
        repo_path="meetingapp (Android)",
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="files", action="write"),
            ModulePermissionSpec(scope="meeting", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="meeting",
        write_action="write",
        categories=["meeting", "task"],
        record_types=["meeting_transcript", "meeting_summary", "decision", "action_item"],
        routable=True,
        storage_adapter="relational",
    ),
    "memory": AppDefinition(
        slug="memory",
        name="Memory Vault",
        description="Inspect, search, organize, update and forget personal memories.",
        client_type="internal",
        status="live",
        platform_type="aura",
        purpose="Structured per-user memory operations (facts / interpretations / events).",
        sends_to_aura=["memory writes"],
        needs_from_aura=["memory reads", "search"],
        queries_brain=True,
        ingests=True,
        repo_path="server1/app/api/v1/brain.py",
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        # Brain memory is written through the dedicated memory path, never as a
        # routed application record.
        routable=False,
    ),
    "files": AppDefinition(
        slug="files",
        name="AURA Documents / Files",
        description="User document and file storage with per-owner isolation.",
        client_type="internal",
        status="live",
        platform_type="aura",
        purpose="Upload, list, download and delete user-authorized documents.",
        sends_to_aura=["documents"],
        needs_from_aura=["files", "downloads"],
        queries_brain=False,
        ingests=True,
        repo_path="server1/app/api/v1/files.py",
        default_permissions=[
            ModulePermissionSpec(scope="files", action="read"),
            ModulePermissionSpec(scope="files", action="write"),
        ],
        ingest_scope="files",
        ingest_action="write",
        write_scope="files",
        write_action="write",
        categories=["document"],
        record_types=["document"],
        routable=True,
        storage_adapter="object",
    ),
    "notes": AppDefinition(
        slug="notes",
        name="AURA Notes",
        description="Notes and knowledge capture into the user's vault.",
        client_type="mobile",
        status="planned",
        platform_type="aura",
        purpose="Capture notes and useful information into the vault.",
        sends_to_aura=["notes"],
        needs_from_aura=["vault", "search"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="files", action="write"),
            ModulePermissionSpec(scope="notes", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="notes",
        write_action="write",
        categories=["note", "document"],
        record_types=["note", "document"],
        routable=True,
        storage_adapter="relational",
    ),
    "study": AppDefinition(
        slug="study",
        name="AURA Study",
        description="Study/lecture capture organized into knowledge and timeline records.",
        client_type="mobile",
        status="planned",
        platform_type="aura",
        purpose="Turn study/lecture recordings into knowledge and timeline entries.",
        sends_to_aura=["lecture notes", "study records"],
        needs_from_aura=["knowledge", "timeline", "memory"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="timeline", action="write"),
            ModulePermissionSpec(scope="study", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="study",
        write_action="write",
        categories=["study_material", "lecture", "deadline"],
        record_types=[
            "lecture_note",
            "lecture_transcript",
            "study_material",
            "concept",
            "assignment_deadline",
        ],
        routable=True,
        storage_adapter="relational",
    ),
    "diary": AppDefinition(
        slug="diary",
        name="AURA Daily Diary",
        description="Private daily journal entries captured into the user's own memory.",
        client_type="mobile",
        status="planned",
        platform_type="aura",
        purpose="Capture dated personal reflections as interpretation memories and timeline entries.",
        sends_to_aura=["diary entries"],
        needs_from_aura=["memory", "timeline"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="timeline", action="write"),
            ModulePermissionSpec(scope="diary", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="diary",
        write_action="write",
        categories=["diary_event"],
        record_types=["diary_entry", "personal_event"],
        routable=True,
        storage_adapter="relational",
    ),
    "travel": AppDefinition(
        slug="travel",
        name="AURA Travel",
        description="Trip and itinerary records preserved in the user's brain.",
        client_type="mobile",
        status="planned",
        platform_type="aura",
        purpose="Capture trips/itineraries as facts and timeline entries.",
        sends_to_aura=["trip records", "itineraries"],
        needs_from_aura=["memory", "timeline", "files"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="files", action="write"),
            ModulePermissionSpec(scope="travel", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="travel",
        write_action="write",
        categories=["travel"],
        record_types=["trip", "itinerary", "travel_booking"],
        routable=True,
        storage_adapter="relational",
    ),
    "tasks": AppDefinition(
        slug="tasks",
        name="AURA Tasks / Planner",
        description="Tasks and planner consuming action items from meeting and other sources.",
        client_type="web",
        status="planned",
        platform_type="aura",
        purpose="Action items and follow-ups surfaced from authorized sources.",
        sends_to_aura=["tasks", "plans"],
        needs_from_aura=["action items", "memory"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="tasks", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="tasks",
        write_action="write",
        categories=["task", "deadline"],
        record_types=["task", "assignment_deadline"],
        routable=True,
        storage_adapter="relational",
    ),
    "timeline": AppDefinition(
        slug="timeline",
        name="AURA Timeline",
        description="Shared dated-event capability tracking relevant events across apps.",
        client_type="internal",
        status="module",
        platform_type="aura",
        purpose="Track dated events that authorized apps contribute.",
        sends_to_aura=["dated events"],
        needs_from_aura=["timeline events"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="timeline", action="read"),
            ModulePermissionSpec(scope="timeline", action="write"),
        ],
        ingest_scope="timeline",
        ingest_action="write",
        write_scope="timeline",
        write_action="write",
        categories=["calendar_event", "project"],
        record_types=["calendar_event", "project_note"],
        routable=True,
        storage_adapter="relational",
    ),
    "notifications": AppDefinition(
        slug="notifications",
        name="AURA Notifications",
        description="Alerts generated from authorized app events.",
        client_type="internal",
        status="planned",
        platform_type="aura",
        purpose="Delivery of user notifications triggered by app events.",
        sends_to_aura=["notification events"],
        needs_from_aura=["notifications"],
        queries_brain=False,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="notifications", action="read"),
            ModulePermissionSpec(scope="notifications", action="write"),
        ],
        ingest_scope="notifications",
        ingest_action="write",
        # Notification Hub consumes events; it does not store application records.
        routable=False,
    ),
    "workflow": AppDefinition(
        slug="workflow",
        name="AURA Workflow",
        description="Coordination of tasks and events across applications.",
        client_type="internal",
        status="planned",
        platform_type="aura",
        purpose="Orchestrate workflow steps across authorized apps.",
        sends_to_aura=["workflow events"],
        needs_from_aura=["workflow", "tasks"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="workflow", action="read"),
            ModulePermissionSpec(scope="workflow", action="write"),
        ],
        ingest_scope="workflow",
        ingest_action="write",
        write_scope="workflow",
        write_action="write",
        categories=["task", "project"],
        record_types=["task", "project_note"],
        routable=True,
        storage_adapter="relational",
    ),
    "search": AppDefinition(
        slug="search",
        name="AURA Search",
        description="Unified search across memories, files and brain context.",
        client_type="internal",
        status="planned",
        platform_type="aura",
        purpose="Find relevant memory and documents for a user query.",
        sends_to_aura=["search queries"],
        needs_from_aura=["search results"],
        queries_brain=True,
        ingests=False,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="read"),
            ModulePermissionSpec(scope="files", action="read"),
        ],
        # Search is read-only.
        ingest_scope=None,
        routable=False,
    ),
    "knowledge-graph": AppDefinition(
        slug="knowledge-graph",
        name="AURA Knowledge Graph",
        description="Links useful entities and facts in the user's brain.",
        client_type="internal",
        status="module",
        platform_type="aura",
        purpose="Maintain entities and relationships distilled from authorized data.",
        sends_to_aura=["entities", "relationships"],
        needs_from_aura=["knowledge retrieval"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="knowledge", action="read"),
            ModulePermissionSpec(scope="knowledge", action="write"),
        ],
        ingest_scope="knowledge",
        ingest_action="write",
        write_scope="knowledge",
        write_action="write",
        categories=["project", "personal_fact"],
        record_types=["project_note", "memory_fact"],
        routable=True,
        storage_adapter="relational",
    ),
    "chronos": AppDefinition(
        slug="chronos",
        name="Chronos",
        description="AURA platform family marker and knowledge collection (chronos_knowledge).",
        client_type="internal",
        status="module",
        platform_type="chronos",
        purpose="Platform-family capability reserved for Chronos apps/modules.",
        sends_to_aura=["chronos events"],
        needs_from_aura=["chronos knowledge"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="knowledge", action="read"),
            ModulePermissionSpec(scope="timeline", action="write"),
        ],
        # Chronos is a platform-family marker with no running service yet.
        ingest_scope=None,
        routable=False,
    ),
    "health": AppDefinition(
        slug="health",
        name="AURA Health / Wellness",
        description="Possible future integration for wellness events (requires product confirmation).",
        client_type="mobile",
        status="future",
        platform_type="aura",
        purpose="Optional wellness event intake; only collect health data with user consent.",
        sends_to_aura=["wellness events"],
        needs_from_aura=["notifications", "memory"],
        queries_brain=True,
        ingests=True,
        default_permissions=[
            ModulePermissionSpec(scope="brain", action="read"),
            ModulePermissionSpec(scope="memory", action="write"),
            ModulePermissionSpec(scope="notifications", action="read"),
            ModulePermissionSpec(scope="health", action="write"),
        ],
        ingest_scope="memory",
        ingest_action="write",
        write_scope="health",
        write_action="write",
        categories=["health"],
        record_types=["wellness_event", "health_record"],
        routable=True,
        storage_adapter="relational",
    ),
}


def list_catalog() -> list[AppDefinition]:
    """Return the complete app catalog sorted by slug."""
    return [CATALOG[slug] for slug in sorted(CATALOG)]


def get_catalog_app(slug: str) -> AppDefinition | None:
    """Return one catalog entry, or None if unknown."""
    return CATALOG.get(slug)


def list_routable_apps() -> list[AppDefinition]:
    """Return the applications the Brain is allowed to deliver records to.

    "Allowed" means the catalog declares a storage adapter and the required
    record types. Permissions are still checked per user at delivery time.
    """
    return [
        app
        for app in list_catalog()
        if app.routable and app.storage_adapter != "none" and app.record_types
    ]


def find_destinations_for_category(category: str) -> list[AppDefinition]:
    """Return routable applications that accept the given semantic category."""
    return [app for app in list_routable_apps() if category in app.categories]


def get_ingest_permission(slug: str) -> tuple[str, str] | None:
    """Return the ``(scope, action)`` needed to submit input as ``slug``.

    ``None`` means the application cannot submit content into the Brain
    pipeline at all.
    """
    app = CATALOG.get(slug)
    if app is None or app.ingest_scope is None:
        return None
    return (app.ingest_scope, app.ingest_action)


def get_destination_permission(slug: str) -> tuple[str, str] | None:
    """Return the ``(scope, action)`` required to write records into ``slug``."""
    app = CATALOG.get(slug)
    if app is None or app.write_scope is None:
        return None
    return (app.write_scope, app.write_action)


class AppConnectionService:
    """Service for managing a user's authorized app connections."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_connections(self, user: User) -> list[InstalledModule]:
        result = await self.session.execute(
            select(InstalledModule)
            .where(InstalledModule.user_id == user.id)
            .order_by(InstalledModule.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_connection(self, user: User, module_id: uuid.UUID) -> InstalledModule | None:
        result = await self.session.execute(
            select(InstalledModule).where(
                InstalledModule.id == module_id,
                InstalledModule.user_id == user.id,
            )
        )
        return result.scalars().first()

    async def get_connection_by_slug(self, user: User, slug: str) -> InstalledModule | None:
        """Return the user's connection to an application, active or not."""
        result = await self.session.execute(
            select(InstalledModule).where(
                InstalledModule.user_id == user.id,
                InstalledModule.module_slug == slug,
            )
        )
        return result.scalars().first()

    @staticmethod
    def has_permission(module: InstalledModule, scope: str, action: str) -> bool:
        """Return True when the connection grants ``scope:action``."""
        target = PermissionAction(action)
        return any(
            permission.scope == scope and permission.permission == target
            for permission in module.module_permissions
        )

    async def has_active_permission(
        self,
        user: User,
        slug: str,
        scope: str,
        action: str,
    ) -> bool:
        """Return True when the user has an active, matching app permission.

        Used to gate brain forwarding so an app can only trigger inference on
        behalf of a user who has actually authorized it.
        """
        result = await self.session.execute(
            select(ModulePermission.id)
            .join(InstalledModule, ModulePermission.installed_module_id == InstalledModule.id)
            .where(
                InstalledModule.user_id == user.id,
                InstalledModule.module_slug == slug,
                InstalledModule.is_active.is_(True),
                ModulePermission.scope == scope,
                ModulePermission.permission == PermissionAction(action),
            )
        )
        return result.scalars().first() is not None

    async def install(
        self,
        user: User,
        slug: str,
        permissions: list[ModulePermissionSpec] | None = None,
    ) -> InstalledModule:
        """Authorize an app for the user, granting its default permissions."""
        app = get_catalog_app(slug)
        if app is None:
            raise UnknownAppError(f"Unknown app slug: {slug}")
        granted = permissions if permissions is not None else _default_permissions(app)
        _validate_permissions(app, granted)

        existing = await self.session.execute(
            select(InstalledModule).where(
                InstalledModule.user_id == user.id,
                InstalledModule.module_slug == slug,
            )
        )
        if existing.scalars().first() is not None:
            raise AppAlreadyInstalledError(f"App already connected: {slug}")

        module = InstalledModule(
            user_id=user.id,
            module_slug=app.slug,
            module_name=app.name,
            platform_type=PlatformType(app.platform_type),
            is_active=True,
        )
        self.session.add(module)
        await self.session.flush()

        await self._replace_permissions(user, module, granted)
        await self.session.commit()
        return module

    async def update_connection(
        self,
        user: User,
        module_id: uuid.UUID,
        is_active: bool | None = None,
        permissions: list[ModulePermissionSpec] | None = None,
    ) -> InstalledModule | None:
        """Update activation state and/or replace granted permissions."""
        module = await self.get_connection(user, module_id)
        if module is None:
            return None

        if permissions is not None:
            app = get_catalog_app(module.module_slug)
            if app is None:
                raise AppPermissionNotAllowedError(
                    f"Permissions cannot be updated for unregistered app: {module.module_slug}"
                )
            _validate_permissions(app, permissions)

        if is_active is not None:
            module.is_active = bool(is_active)
        if permissions is not None:
            await self._replace_permissions(user, module, permissions)
        await self.session.commit()
        return module

    async def revoke(self, user: User, module_id: uuid.UUID) -> bool:
        """Revoke the user's connection to an app (soft-delete is not used)."""
        module = await self.get_connection(user, module_id)
        if module is None:
            return False
        await self.session.delete(module)
        await self.session.commit()
        return True

    async def _replace_permissions(
        self,
        user: User,
        module: InstalledModule,
        permissions: list[ModulePermissionSpec],
    ) -> None:
        """Replace the module's permission set and persist it.

        Uses ``set_committed_value`` to populate the in-memory relationship
        without triggering an implicit lazy load (which is not allowed on async
        sessions), so the caller can serialise the module directly.
        """
        await self.session.execute(
            delete(ModulePermission).where(ModulePermission.installed_module_id == module.id)
        )
        granted = [
            ModulePermission(
                user_id=user.id,
                installed_module_id=module.id,
                scope=spec.scope,
                permission=PermissionAction(spec.action),
            )
            for spec in permissions
        ]
        for permission in granted:
            self.session.add(permission)
        set_committed_value(module, "module_permissions", granted)
        await self.session.flush()


def _default_permissions(app: AppDefinition) -> list[ModulePermissionSpec]:
    return list(app.default_permissions)


def _validate_permissions(app: AppDefinition, permissions: list[ModulePermissionSpec]) -> None:
    """Reject grants the app registry has not explicitly approved."""
    allowed = {
        (permission.scope, permission.action)
        for permission in app.default_permissions + app.additional_permissions
    }
    for permission in permissions:
        grant = (permission.scope, permission.action)
        if grant not in allowed:
            raise AppPermissionNotAllowedError(
                f"Permission is not allowed for {app.slug}: {permission.scope}:{permission.action}"
            )