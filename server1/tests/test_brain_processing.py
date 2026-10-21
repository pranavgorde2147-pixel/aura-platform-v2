"""Tests for the universal Brain processing pipeline.

These tests cover the policy decisions (risk tiers, confidence thresholds,
consent), the registry-driven destination resolution, the authorization
checks, the idempotent storage adapters and the orchestration itself.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.brain_memory import MemoryKind
from app.models.app_record import AppRecordLink
from app.models.brain_processing import (
    BrainProcessingOperation,
    BrainProcessingRun,
    OperationStatus,
    ProcessingStatus,
)
from app.models.module_permission import PermissionAction
from app.schemas.processing import ProcessingRequest
from app.services.classification_policy import (
    ClassificationPolicy,
    RiskTier,
    SensitiveContentDetector,
)
from app.services.content_analysis import AnalysisResult, AnalysisSegment
from app.services.destination_resolver import DestinationResolver
from app.services.storage_adapters import (
    RecordPayload,
    RelationalRecordStore,
    build_dedupe_key,
)


# ---------------------------------------------------------------- fixtures


class FakeModule:
    """A connected application with a set of granted permissions."""

    def __init__(self, slug: str, permissions=("memory:write",), active: bool = True) -> None:
        self.module_slug = slug
        self.is_active = active
        self.module_permissions = [
            SimpleNamespace(scope=scope, permission=PermissionAction(action))
            for scope, action in (item.split(":") for item in permissions)
        ]


class FakeConnections:
    """Minimal stand-in for ``AppConnectionService``."""

    def __init__(self, modules: list[FakeModule]) -> None:
        self.modules = {module.module_slug: module for module in modules}

    async def get_connection_by_slug(self, user, slug):
        return self.modules.get(slug)

    async def has_active_permission(self, user, slug, scope, action):
        module = self.modules.get(slug)
        return bool(
            module
            and module.is_active
            and self.has_permission(module, scope, action)
        )

    @staticmethod
    def has_permission(module, scope, action) -> bool:
        target = PermissionAction(action)
        return any(
            permission.scope == scope and permission.permission == target
            for permission in module.module_permissions
        )


class FakeRecordRepository:
    """In-memory replacement for ``AppRecordRepository``."""

    def __init__(self) -> None:
        self.records: dict[tuple, SimpleNamespace] = {}
        self.links: list[SimpleNamespace] = []

    def track(self, record) -> None:
        self.records[
            (record.user_id, record.destination_app, record.record_type, record.dedupe_key)
        ] = record

    async def find_by_dedupe(self, *, user_id, destination_app, record_type, dedupe_key):
        return self.records.get((user_id, destination_app, record_type, dedupe_key))

    async def get_for_user(self, *, user_id, record_id):
        return next(
            (record for record in self.records.values() if record.user_id == user_id and record.id == record_id),
            None,
        )

    async def find_by_source_reference(self, *, user_id, source_app, source_reference):
        return next(
            (
                record
                for record in self.records.values()
                if record.user_id == user_id
                and record.destination_app == source_app
                and record.source_reference == source_reference
            ),
            None,
        )

    async def find_app_link(self, *, user_id, record_id, target_app, relation_type):
        return next(
            (
                link for link in self.links
                if link.user_id == user_id and link.record_id == record_id
                and link.target_app == target_app and link.relation_type == relation_type
            ),
            None,
        )

    async def find_record_link(self, *, user_id, record_id, related_record_id, relation_type):
        return next(
            (
                link for link in self.links
                if link.user_id == user_id and link.record_id == record_id
                and link.related_record_id == related_record_id
                and link.relation_type == relation_type
            ),
            None,
        )

    async def list_by_run(self, run_id):
        return [record for record in self.records.values() if record.source_run_id == run_id]


class FakeSession:
    """Session double that assigns ids on ``add`` like a real flush would."""

    def __init__(self, repository: FakeRecordRepository | None = None) -> None:
        self.repository = repository
        self.added: list = []

    def add(self, record) -> None:
        if getattr(record, "id", None) is None:
            record.id = uuid.uuid4()
        self.added.append(record)
        if self.repository is not None and hasattr(record, "dedupe_key"):
            self.repository.track(record)
        if self.repository is not None and isinstance(record, AppRecordLink):
            self.repository.links.append(record)

    async def flush(self) -> None:
        return None


def make_user():
    return SimpleNamespace(id=uuid.uuid4(), email="pipeline@example.com")


def make_policy(**overrides) -> ClassificationPolicy:
    policy = ClassificationPolicy.from_settings()
    if not overrides:
        return policy
    return ClassificationPolicy(**{**policy.__dict__, **overrides})


def make_analysis(
    segments: list[AnalysisSegment],
    *,
    analysis_mode: str = "llm",
    confidence_kind: str = "model",
) -> AnalysisResult:
    return AnalysisResult(
        segments=segments,
        analysis_mode=analysis_mode,
        confidence_kind=confidence_kind,
        provider="test",
    )


def segment(
    index: int = 0,
    category: str = "lecture",
    record_type: str = "lecture_note",
    confidence: float = 0.9,
    text: str = "Lecture on normalisation",
) -> AnalysisSegment:
    return AnalysisSegment(
        index=index,
        category=category,
        record_type=record_type,
        text=text,
        summary=text[:200],
        confidence=confidence,
        rationale="test",
    )


class FakeRunRepository:
    """In-memory replacement for ``BrainProcessingRunRepository``."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    @property
    def _runs(self):
        return [
            item
            for item in self.session.added
            if isinstance(item, BrainProcessingRun)
        ]

    async def find_by_idempotency_key(self, *, user_id, idempotency_key):
        for run in self._runs:
            if run.idempotency_key == idempotency_key and run.user_id == user_id:
                return run
        return None

    async def get_for_user(self, *, user_id, run_id):
        for run in self._runs:
            if run.id == run_id and run.user_id == user_id:
                return run
        return None


class FakeOperationRepository:
    """In-memory replacement for ``BrainProcessingOperationRepository``."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def list_for_run(self, run_id):
        return [
            item
            for item in self.session.added
            if isinstance(item, BrainProcessingOperation) and item.run_id == run_id
        ]


class FakeMemoryRepository:
    """In-memory replacement for ``BrainMemoryRepository``."""

    def __init__(self) -> None:
        self.created: list = []

    async def create(self, **kwargs):
        memory = SimpleNamespace(id=uuid.uuid4(), **kwargs)
        self.created.append(memory)
        return memory


class FakeIdentityRepository:
    """In-memory replacement for ``AIIdentityRepository``."""

    def __init__(self, identity_id: uuid.UUID | None) -> None:
        self.identity_id = identity_id

    async def get_by_user_id(self, user_id):
        if self.identity_id is None:
            return None
        return SimpleNamespace(id=self.identity_id)


def build_router(
    *,
    modules: list[FakeModule],
    analysis_result: AnalysisResult,
    policy: ClassificationPolicy | None = None,
    identity_id: uuid.UUID | None = None,
):
    """Build a router wired to in-memory doubles."""
    from app.services.brain_router import BrainRouter

    repository = FakeRecordRepository()
    session = FakeSession(repository)
    router = BrainRouter(
        session,
        policy=policy or ClassificationPolicy.from_settings(),
        connections=FakeConnections(modules),
        analysis=SimpleNamespace(analyze=AsyncMock(return_value=analysis_result)),
    )
    router.run_repo = FakeRunRepository(session)
    router.operation_repo = FakeOperationRepository(session)
    router.record_repo = repository
    router.memory_repo = FakeMemoryRepository()
    router.identity_repo = FakeIdentityRepository(identity_id)
    session.commit = AsyncMock()
    return router, session


# ----------------------------------------------------------------- policy


def test_risk_tiers_drive_the_confidence_threshold():
    policy = ClassificationPolicy.from_settings()
    assert policy.risk_tier("note") is RiskTier.LOW
    assert policy.risk_tier("lecture") is RiskTier.MEDIUM
    assert policy.risk_tier("calendar_event") is RiskTier.HIGH

    assert policy.threshold_for("note") == policy.note_threshold
    assert policy.threshold_for("lecture") == policy.medium_threshold
    assert policy.threshold_for("health") == policy.sensitive_threshold
    assert policy.note_threshold < policy.medium_threshold < policy.sensitive_threshold


def test_unclassified_items_are_never_delivered_automatically():
    policy = ClassificationPolicy.from_settings()
    assert policy.accepts("unclassified", 1.0) is False
    assert policy.accepts("note", 0.99) is True
    assert policy.accepts("health", 0.99) is True


def test_explicit_instruction_lowers_the_bar_but_not_for_unclassified():
    policy = ClassificationPolicy.from_settings()
    assert policy.accepts("health", 0.1, explicit=True) is True
    assert policy.accepts("unclassified", 1.0, explicit=True) is False


def test_high_risk_categories_require_confirmation():
    policy = ClassificationPolicy.from_settings()
    assert policy.requires_confirmation("health") is True
    assert policy.requires_confirmation("note") is False


def test_memory_is_only_allowed_for_facts_and_preferences():
    policy = ClassificationPolicy.from_settings()
    assert policy.memory_allowed("personal_fact") is True
    assert policy.memory_allowed("user_preference") is True
    assert policy.memory_allowed("health") is False
    assert policy.memory_allowed("meeting") is False


def test_memory_can_be_disabled_globally():
    policy = make_policy(memory_write_enabled=False)
    assert policy.memory_allowed("personal_fact") is False


def test_sensitive_content_detector_finds_credentials():
    detector = SensitiveContentDetector()
    assert detector.contains_secret("my password is hunter2") is True
    assert detector.contains_secret("api_key: abcdef123456") is True
    assert detector.contains_secret("we discussed the lecture notes") is False
    assert detector.contains_secret(None) is False


# ------------------------------------------------------------ destinations


def test_destinations_come_from_the_registry_not_from_the_text():
    resolver = DestinationResolver(FakeConnections([]))
    candidates = resolver.candidates_for("lecture", "lecture_note")
    assert [candidate.app.slug for candidate in candidates] == ["study"]


def test_requested_destinations_restrict_the_candidate_set():
    resolver = DestinationResolver(FakeConnections([]))
    candidates = resolver.candidates_for("task", "task", ["meeting"])
    assert [candidate.app.slug for candidate in candidates] == ["meeting"]


def test_unknown_category_has_no_destination():
    resolver = DestinationResolver(FakeConnections([]))
    assert resolver.candidates_for("unclassified", "note") == []


def test_a_destination_without_the_exact_record_type_uses_the_closest_one():
    resolver = DestinationResolver(FakeConnections([]))
    candidates = resolver.candidates_for("lecture", "note")
    assert candidates
    candidate = candidates[0]
    assert candidate.app.slug == "study"
    assert candidate.exact_match is False
    assert candidate.record_type in candidate.app.record_types


def test_destination_authorization_requires_a_granted_permission():
    connections = FakeConnections([FakeModule("study", permissions=("memory:write",))])
    resolver = DestinationResolver(connections)

    verdict = asyncio.run(resolver.authorize_destination(make_user(), "study", "lecture_note"))
    assert verdict.allowed is False
    assert verdict.reason == "permission_missing"
    assert verdict.required_permission == "study:write"


def test_destination_authorization_rejects_a_missing_connection():
    resolver = DestinationResolver(FakeConnections([]))
    verdict = asyncio.run(resolver.authorize_destination(make_user(), "study", "lecture_note"))
    assert verdict.allowed is False
    assert verdict.reason == "connection_missing"


def test_destination_authorization_rejects_an_inactive_connection():
    connections = FakeConnections([FakeModule("study", permissions=("study:write",), active=False)])
    resolver = DestinationResolver(connections)
    verdict = asyncio.run(resolver.authorize_destination(make_user(), "study", "lecture_note"))
    assert verdict.allowed is False
    assert verdict.reason == "connection_inactive"


def test_source_authorization_requires_the_declared_ingest_permission():
    connections = FakeConnections([FakeModule("study", permissions=("memory:write",))])
    resolver = DestinationResolver(connections)

    allowed = asyncio.run(resolver.authorize_source(make_user(), "study"))
    assert allowed.allowed is True

    missing = asyncio.run(resolver.authorize_source(make_user(), "notes"))
    assert missing.allowed is False
    assert missing.reason == "connection_missing"

    unknown = asyncio.run(resolver.authorize_source(make_user(), "not-an-app"))
    assert unknown.allowed is False
    assert unknown.reason == "unknown_app"


# --------------------------------------------------------------- adapters


def test_relational_store_is_idempotent_for_the_same_dedupe_key():
    repository = FakeRecordRepository()
    session = FakeSession(repository)
    store = RelationalRecordStore(session, repository)

    payload = RecordPayload(
        user_id=uuid.uuid4(),
        destination_app="study",
        record_type="lecture_note",
        content="Lecture on normalisation",
        dedupe_key="key-1",
    )

    async def _run():
        first = await store.write(payload)
        second = await store.write(payload)
        return first, second

    first, second = asyncio.run(_run())
    assert first.created is True
    assert second.created is False
    assert first.record_id == second.record_id
    assert first.record_id is not None
    assert session.added and len(session.added) == 1


def test_dedupe_key_separates_users_destinations_and_content():
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    key_a = build_dedupe_key(
        user_id=user_a,
        destination_app="study",
        record_type="lecture_note",
        run_id=None,
        segment_index=0,
        content="one",
    )
    key_b = build_dedupe_key(
        user_id=user_b,
        destination_app="study",
        record_type="lecture_note",
        run_id=None,
        segment_index=0,
        content="one",
    )
    key_c = build_dedupe_key(
        user_id=user_a,
        destination_app="study",
        record_type="lecture_note",
        run_id=None,
        segment_index=0,
        content="two",
    )
    assert key_a != key_b
    assert key_a != key_c


# -------------------------------------------------------------- orchestration


def test_router_delivers_a_confident_item_to_the_authorized_destination():
    async def _run():
        router, session = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis([segment(category="lecture", confidence=0.92)]),
        )
        outcome = await router.process(
            make_user(),
            ProcessingRequest(
                content="Lecture on normalisation",
                source_app="study",
                input_id="lecture-1",
            ),
        )
        return router, session, outcome

    router, session, outcome = asyncio.run(_run())

    assert outcome.run.status == ProcessingStatus.DELIVERED
    assert outcome.run.review_required is False
    assert len(outcome.operations) == 1
    operation = outcome.operations[0]
    assert operation.destination_app == "study"
    assert operation.status == OperationStatus.DELIVERED
    assert operation.record_reference
    assert len(session.added) >= 2  # the run and the stored record
    stored = [item for item in session.added if hasattr(item, "dedupe_key")]
    assert stored[0].destination_app == "study"
    assert stored[0].content == "Lecture on normalisation"


def test_router_parks_a_destination_without_permission_for_authorization():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write",))],
            analysis_result=make_analysis([segment(category="lecture", confidence=0.92)]),
        )
        return await router.process(
            make_user(),
            ProcessingRequest(
                content="Lecture on normalisation",
                source_app="study",
                input_id="lecture-2",
            ),
        )

    outcome = asyncio.run(_run())

    assert outcome.run.status == ProcessingStatus.AWAITING_REVIEW
    assert outcome.run.review_required is True
    operation = outcome.operations[0]
    assert operation.status == OperationStatus.AWAITING_AUTHORIZATION
    assert operation.error_code == "permission_missing"
    assert operation.record_reference is None


def test_router_requires_review_when_the_confidence_is_too_low():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis([segment(category="lecture", confidence=0.55)]),
        )
        return await router.process(
            make_user(),
            ProcessingRequest(
                content="maybe a lecture",
                source_app="study",
                input_id="lecture-3",
            ),
        )

    outcome = asyncio.run(_run())

    assert outcome.run.status == ProcessingStatus.AWAITING_REVIEW
    operation = outcome.operations[0]
    assert operation.status == OperationStatus.AWAITING_REVIEW
    assert operation.requires_review is True


def test_router_rejects_a_source_app_the_user_did_not_connect():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis([segment()]),
        )
        return await router.process(
            make_user(),
            ProcessingRequest(content="hello", source_app="notes", input_id="x"),
        )

    from app.services.brain_router import ProcessingError

    with pytest.raises(ProcessingError) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.code == "connection_missing"
    assert excinfo.value.status_code == 403


def test_router_does_not_write_memory_without_an_explicit_opt_in():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis(
                [segment(category="personal_fact", record_type="fact", confidence=0.95)]
            ),
            identity_id=uuid.uuid4(),
        )
        return await router.process(
            make_user(),
            ProcessingRequest(
                content="I prefer dark mode",
                source_app="study",
                input_id="pref-1",
            ),
        )

    outcome = asyncio.run(_run())
    assert outcome.memories == []


def test_router_writes_a_bounded_memory_when_the_user_opts_in():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis(
                [
                    segment(
                        category="personal_fact",
                        record_type="fact",
                        confidence=0.95,
                        text="I prefer dark mode",
                    )
                ]
            ),
            identity_id=uuid.uuid4(),
        )
        return await router.process(
            make_user(),
            ProcessingRequest(
                content="I prefer dark mode",
                source_app="study",
                input_id="pref-2",
                persist_memory=True,
                consent={"memory_write": True},
            ),
        )

    outcome = asyncio.run(_run())
    assert len(outcome.memories) == 1
    assert outcome.memories[0].kind == MemoryKind.FACT
    assert outcome.memories[0].content == "I prefer dark mode"


def test_router_never_stores_a_credential_in_memory():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis(
                [
                    segment(
                        category="personal_fact",
                        record_type="fact",
                        confidence=0.95,
                        text="my password is hunter2",
                    )
                ]
            ),
            identity_id=uuid.uuid4(),
        )
        return await router.process(
            make_user(),
            ProcessingRequest(
                content="my password is hunter2",
                source_app="study",
                input_id="secret-1",
                persist_memory=True,
                consent={"memory_write": True},
            ),
        )

    outcome = asyncio.run(_run())
    assert outcome.memories == []
    assert any(error["code"] == "sensitive_content_skipped" for error in outcome.errors)


def test_router_is_idempotent_for_the_same_idempotency_key():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis([segment(category="lecture", confidence=0.92)]),
        )
        request = ProcessingRequest(
            content="Lecture on normalisation",
            source_app="study",
            input_id="lecture-4",
        )
        user = make_user()
        first = await router.process(user, request, idempotency_key="key-1")
        second = await router.process(user, request, idempotency_key="key-1")
        return first, second

    first, second = asyncio.run(_run())
    assert second.duplicate is True
    assert second.run.id == first.run.id


def test_cross_app_routing_requires_explicit_consent_and_links_the_source_record():
    async def _run(cross_app_routing: bool):
        modules = [
            FakeModule("diary", permissions=("memory:write", "diary:write")),
            FakeModule("study", permissions=("memory:write", "study:write")),
        ]
        router, session = build_router(
            modules=modules,
            analysis_result=make_analysis(
                [
                    segment(
                        category="lecture",
                        record_type="lecture_note",
                        text="A lecture captured in the diary app",
                    )
                ]
            ),
        )
        outcome = await router.process(
            make_user(),
            ProcessingRequest(
                content="A lecture captured in the diary app",
                source_app="diary",
                input_id=f"cross-app-{cross_app_routing}",
                requested_destinations=["study"],
                consent={"cross_app_routing": cross_app_routing},
            ),
        )
        return outcome, session

    denied, _ = asyncio.run(_run(False))
    assert denied.operations[0].destination_app == "diary"
    assert denied.operations[0].status is OperationStatus.SKIPPED
    assert denied.source_record is not None

    routed, session = asyncio.run(_run(True))
    assert routed.operations[0].destination_app == "study"
    assert routed.operations[0].status is OperationStatus.DELIVERED
    assert routed.source_record is not None
    assert routed.operations[0].record_reference == str(routed.source_record.id)
    links = [item for item in session.added if isinstance(item, AppRecordLink)]
    assert len(links) == 1
    assert links[0].record_id == routed.source_record.id
    assert links[0].target_app == "study"
    assert len([item for item in session.added if hasattr(item, "dedupe_key")]) == 1


def test_source_reference_cannot_be_reused_for_different_content():
    from app.models.app_record import AppRecord
    from app.services.brain_router import ProcessingError

    async def _run():
        router, _ = build_router(
            modules=[FakeModule("diary", permissions=("memory:write", "diary:write"))],
            analysis_result=make_analysis([segment()]),
        )
        user = make_user()
        existing = AppRecord(
            user_id=user.id,
            destination_app="diary",
            record_type="source_record",
            content="The original content",
            source_reference="upstream-1",
            dedupe_key="old-key",
        )
        router.record_repo.track(existing)
        with pytest.raises(ProcessingError) as exc:
            await router._store_source_record(
                user,
                ProcessingRequest(
                    content="Different content",
                    source_app="diary",
                    content_ref="upstream-1",
                ),
                SimpleNamespace(id=uuid.uuid4(), ai_identity_id=None),
                make_analysis([segment()]),
            )
        return exc.value

    error = asyncio.run(_run())
    assert error.code == "source_reference_conflict"
    assert error.status_code == 409


def test_confirm_delivers_the_items_the_user_approved():
    async def _run():
        router, _ = build_router(
            modules=[FakeModule("study", permissions=("memory:write", "study:write"))],
            analysis_result=make_analysis([segment(category="lecture", confidence=0.55)]),
        )
        user = make_user()
        outcome = await router.process(
            user,
            ProcessingRequest(
                content="maybe a lecture",
                source_app="study",
                input_id="lecture-5",
            ),
        )
        from app.schemas.processing import ConfirmItem

        confirmed = await router.confirm(user, outcome.run.id, [ConfirmItem(index=0)])
        return confirmed

    confirmed = asyncio.run(_run())
    assert confirmed.run.status == ProcessingStatus.DELIVERED
    assert confirmed.operations[0].status == OperationStatus.DELIVERED
    assert confirmed.operations[0].record_reference


# ------------------------------------------------------------------ taxonomy


def _load_server2_analysis_module():
    """Load Server 2's taxonomy module by path, if the sibling service exists."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "server2" / "app" / "analysis.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("aura_server2_analysis", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server1_and_server2_share_the_same_taxonomy():
    """The two services must agree on categories and record types.

    A record type that Server 2 produced and Server 1 rewrote would silently
    change the destination of an item.
    """
    from app.services.content_analysis import VALID_CATEGORIES, VALID_RECORD_TYPES

    module = _load_server2_analysis_module()
    if module is None:  # pragma: no cover - only when the sibling service is absent
        pytest.skip("Server 2 analysis module is not available")

    assert VALID_CATEGORIES == frozenset(module.CATEGORIES)
    assert VALID_RECORD_TYPES == frozenset(module.RECORD_TYPES)
