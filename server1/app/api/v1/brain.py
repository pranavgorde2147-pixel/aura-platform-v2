"""Personal Brain API boundary for app-to-brain communication."""

from __future__ import annotations

import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.models.user import User
from app.schemas.brain import (
    BrainIngestRequest,
    BrainIngestResponse,
    BrainProvisioningResponse,
    BrainQueryRequest,
    BrainQueryResponse,
    MemoryCreateRequest,
    MemoryResponse,
    MemoryUpdateRequest,
)
from app.services.app_registry import AppConnectionService
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository

router = APIRouter(prefix="/brain", tags=["Brain"])
_HARNESS_PREFIX = re.compile(r"^\s*harness\s*:\s*", re.IGNORECASE)


async def _vault_brain(session: AsyncSession, user: User):
    identity = await AIIdentityRepository(session).get_by_user_id(user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if identity is None or brain is None:
        raise HTTPException(status_code=404, detail="Personal Brain not found")
    if brain.provisioning_status != "ready":
        raise HTTPException(status_code=503, detail="Personal Brain is not ready")
    return identity, brain


async def _vault_call(user: User, brain_id: uuid.UUID, **kwargs) -> dict:
    try:
        return await get_brain_client().vault_request(
            user_id=str(user.id), brain_id=str(brain_id), **kwargs
        )
    except BrainServiceError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Brain record not found") from exc
        if exc.status_code == 409:
            raise HTTPException(status_code=409, detail="Brain record conflict") from exc
        raise HTTPException(
            status_code=503, detail="Personal Brain storage is unavailable"
        ) from exc


def _memory_response(record: dict, identity_id: uuid.UUID) -> MemoryResponse:
    metadata = record.get("metadata") or {}
    return MemoryResponse(
        id=uuid.UUID(str(record["record_id"])),
        ai_identity_id=identity_id,
        kind=metadata.get("kind", "fact"),
        source="brain",
        content=record.get("content") or "",
        summary=metadata.get("summary"),
        source_app=record.get("source_app"),
        importance=int(metadata.get("importance", 0)),
        created_at=record.get("created_at"),
    )


def _query_namespaces(source_app: str | None, connections: list) -> list[str]:
    """Return this caller's connected app namespaces, never another owner's."""
    namespaces = {"shared", "conversations"}

    for module in connections:
        if not module.is_active:
            continue
        if AppConnectionService.has_permission(module, "brain", "read"):
            namespaces.add(f"apps/{module.module_slug}")
        if module.module_slug == "files" and AppConnectionService.has_permission(
            module, "files", "read"
        ):
            namespaces.add("documents")
    return sorted(namespaces)


def _management_namespaces(connections: list) -> list[str]:
    """Return every app namespace belonging to this user's Brain."""
    namespaces = {"shared", "conversations", "documents"}
    for module in connections:
        slug = module.module_slug
        if isinstance(slug, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug):
            namespaces.add(f"apps/{slug}")
    return sorted(namespaces)


@router.get("/me", response_model=BrainProvisioningResponse)
async def my_brain(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainProvisioningResponse:
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if identity is None or brain is None:
        raise HTTPException(status_code=404, detail="Personal Brain is not provisioned")
    return BrainProvisioningResponse(
        brain_id=brain.id,
        ai_identity_id=identity.id,
        provisioning_status=brain.provisioning_status,
        resource_manifest_version=brain.resource_manifest_version,
        model_configuration=brain.model_configuration,
        inference_runtime_status=brain.inference_runtime_status,
        compute_allocation=brain.compute_allocation,
        storage_resource_reference=brain.storage_resource_reference,
        last_health_check=brain.last_health_check,
        provisioning_error=brain.provisioning_error,
        llm_runtime_id=brain.llm_runtime_id,
        runtime_isolation=brain.runtime_isolation,
    )


@router.post("/me/reconcile", response_model=BrainProvisioningResponse)
async def reconcile_my_brain(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainProvisioningResponse:
    """Retry idempotent Server 2 registration for this authenticated owner."""
    from app.api.v1.users import (
        _provision_primary_brain,
        _sync_brain_app_connections,
    )

    await _provision_primary_brain(session, current_user)
    await _sync_brain_app_connections(session, current_user)
    return await my_brain(current_user=current_user, session=session)


@router.post("/query", response_model=BrainQueryResponse)
async def query_brain(
    payload: BrainQueryRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainQueryResponse:
    """Process a user query through their Personal Brain."""
    harness_mode = _HARNESS_PREFIX.match(payload.query) is not None
    management_request = (
        payload.manage_vault
        or harness_mode
        or payload.delete_approval_id is not None
        or payload.delete_plan_cancel_id is not None
        or payload.bulk_replace_approval_id is not None
        or payload.bulk_replace_cancel_id is not None
    )
    if sum(
        value is not None
        for value in (
            payload.delete_approval_id,
            payload.delete_plan_cancel_id,
            payload.bulk_replace_approval_id,
            payload.bulk_replace_cancel_id,
        )
    ) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Submit only one plan approval or cancellation per request",
        )
    if management_request and not payload.source_app:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source_app is required for Brain vault management",
        )
    if payload.manage_vault and payload.source_app != "aura-core":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Whole-vault management is available only to AURA Core",
        )
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if payload.source_app:
        authorized = await AppConnectionService(session).has_active_permission(
            current_user, payload.source_app, scope="brain", action="read"
        )
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="App is not authorized to query this user's brain",
            )
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The user's Brain is not ready; retry provisioning before querying.",
        )

    request_id = str(payload.request_id or uuid.uuid4())
    query_namespace = (
        f"apps/{payload.source_app}" if payload.source_app else "conversations"
    )
    query_source_app = payload.source_app or "brain"
    namespaces = ["shared"]
    # Harness tool access stays app-local; query retrieval can still provide
    # separately authorized cross-app records as read-only context.
    search_namespaces = namespaces
    if management_request:
        if payload.manage_vault:
            connection_service = AppConnectionService(session)
            app_connections = await connection_service.list_connections(current_user)
            namespaces = _management_namespaces(app_connections)
            search_namespaces = namespaces
        elif payload.source_app:
            namespaces = [f"apps/{payload.source_app}"]
            search_namespaces = (
                _query_namespaces(
                    payload.source_app,
                    await AppConnectionService(session).list_connections(current_user),
                )
                if payload.use_memory
                else namespaces
            )
    elif payload.use_memory:
        connection_service = AppConnectionService(session)
        app_connections = await connection_service.list_connections(current_user)
        namespaces = _query_namespaces(payload.source_app, app_connections)
        search_namespaces = namespaces
    client = get_brain_client()
    records: list[dict] = []
    operation = "vault operation"
    persisted_record_id: str | None = None
    persisted_record_version: int | None = None
    try:
        if payload.request_id is not None:
            operation = "vault idempotency lookup"
            previous = await client.vault_request(
                user_id=str(current_user.id),
                brain_id=str(brain.id),
                method="GET",
                path="records",
                params={
                    "namespace": query_namespace,
                    "source_reference": request_id,
                    "limit": 2,
                },
            )
            prior_items = previous.get("items", [])
            if not prior_items and payload.source_app:
                # Preserve idempotent retries for turns written before query
                # records were stored in their source app namespace.
                operation = "legacy conversation lookup"
                previous = await client.vault_request(
                    user_id=str(current_user.id),
                    brain_id=str(brain.id),
                    method="GET",
                    path="records",
                    params={
                        "namespace": "conversations",
                        "source_reference": request_id,
                        "limit": 2,
                    },
                )
                prior_items = previous.get("items", [])
            if prior_items:
                prior = prior_items[0]
                metadata = prior.get("metadata") or {}
                if (
                    metadata.get("query") != payload.query
                    or metadata.get("source_app") != payload.source_app
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="request_id was already used for a different query",
                    )
                prior_answer = metadata.get("answer")
                if prior_answer and metadata.get("interaction_status") != "pending":
                    prior_records = metadata.get("used_records") or []
                    record_references = [
                        {
                            "id": uuid.UUID(str(item["record_id"])),
                            "source_app": item.get("source_app") or "unknown",
                            "record_type": item.get("record_type") or "record",
                            "source_reference": item.get("source_reference"),
                            "content": item.get("content") or "",
                        }
                        for item in prior_records
                        if item.get("record_id")
                    ]
                    return BrainQueryResponse(
                        answer=str(prior_answer),
                        request_id=payload.request_id,
                        source=str(metadata.get("source") or "server2_brain"),
                        context_used=bool(prior_records or payload.context),
                        user_id=str(current_user.id),
                        model=metadata.get("model"),
                        tokens_used=metadata.get("tokens_used"),
                        records_used=record_references,
                        persisted_record_id=uuid.UUID(str(prior["record_id"])),
                        pending_delete_plans=metadata.get("pending_delete_plans") or [],
                        delete_result=metadata.get("delete_result"),
                        pending_bulk_replace_plans=(
                            metadata.get("pending_bulk_replace_plans") or []
                        ),
                        bulk_replace_result=metadata.get("bulk_replace_result"),
                    )
                persisted_record_id = str(prior["record_id"])
                persisted_record_version = int(prior["version"])

        if persisted_record_id is None:
            operation = "conversation query save"
            staged = await client.vault_request(
                user_id=str(current_user.id),
                brain_id=str(brain.id),
                method="POST",
                path="records",
                json_body={
                    "record_type": "conversation_turn",
                    "namespace": query_namespace,
                    "idempotency_key": (
                        f"query:{request_id}"
                        if query_namespace == "conversations"
                        else f"query:{payload.source_app}:{request_id}"
                    ),
                    "title": payload.query[:120],
                    "content": f"User: {payload.query}",
                    "source_app": query_source_app,
                    "source_reference": request_id,
                    "metadata": {
                        "query": payload.query,
                        "answer": None,
                        "source_app": payload.source_app,
                        "interaction_status": "pending",
                        "used_records": [],
                    },
                },
            )
            persisted_record_id = str(staged["record_id"])
            persisted_record_version = int(staged["version"])

        if payload.use_memory:
            operation = "vault search"
            search_result = await client.vault_request(
                user_id=str(current_user.id),
                brain_id=str(brain.id),
                method="POST",
                path="search",
                json_body={
                    "query": payload.query,
                    "limit": payload.memory_limit,
                    "namespaces": sorted(set(search_namespaces)),
                },
            )
            records = search_result.get("results", [])
            records = [
                item
                for item in records
                if str(item.get("record_id")) != persisted_record_id
            ]
        enriched_context = {
            key: value
            for key, value in (payload.context or {}).items()
            if key.lower() not in {
                "vault",
                "password",
                "secret",
                "token",
                "jwt",
                "authorized_records",
                "authorized_memories",
                "relevant_memories",
                "known_facts",
                "authorized_namespaces",
                "confirm_tool_writes",
                "confirm_tool_deletes",
                "confirmed_delete_scope",
                "confirmed_delete_count",
                "delete_approval_id",
                "delete_plan_cancel_id",
                "bulk_replace_approval_id",
                "bulk_replace_cancel_id",
                "manage_vault",
                "harness_mode",
                "source_app",
            }
        }
        enriched_context["source_app"] = payload.source_app
        enriched_context["authorized_namespaces"] = namespaces
        enriched_context["confirm_tool_writes"] = (
            payload.confirm_tool_writes or harness_mode
        )
        enriched_context["delete_approval_id"] = (
            str(payload.delete_approval_id) if payload.delete_approval_id else None
        )
        enriched_context["delete_plan_cancel_id"] = (
            str(payload.delete_plan_cancel_id)
            if payload.delete_plan_cancel_id
            else None
        )
        enriched_context["bulk_replace_approval_id"] = (
            str(payload.bulk_replace_approval_id)
            if payload.bulk_replace_approval_id
            else None
        )
        enriched_context["bulk_replace_cancel_id"] = (
            str(payload.bulk_replace_cancel_id)
            if payload.bulk_replace_cancel_id
            else None
        )
        enriched_context["manage_vault"] = payload.manage_vault
        enriched_context["harness_mode"] = harness_mode
        authorized_records = [
            {
                "record_id": item.get("record_id"),
                "source_app": item.get("source_app"),
                "record_type": item.get("record_type"),
                "source_reference": item.get("source_reference"),
                "content": str(item.get("content") or "")[:4000],
            }
            for item in records
        ]
        if authorized_records:
            enriched_context["authorized_records"] = authorized_records
        start = time.perf_counter()
        operation = "Harness"
        deterministic_harness_request = harness_mode or any(
            value is not None
            for value in (
                payload.delete_approval_id,
                payload.delete_plan_cancel_id,
                payload.bulk_replace_approval_id,
                payload.bulk_replace_cancel_id,
            )
        )
        inference = (
            await client.harness(
                str(current_user.id),
                payload.query,
                enriched_context,
                request_id=request_id,
            )
            if deterministic_harness_request
            else {"supported": False}
        )
        if inference.get("supported") is not True:
            operation = "inference"
            inference = await client.infer(
                str(current_user.id),
                payload.query,
                enriched_context,
                request_id=request_id,
            )
        answer = str(inference.get("response") or "")
        if not answer:
            raise BrainServiceError(
                "brain_invalid_response", "AURA Brain returned an empty response"
            )
        operation = "conversation answer save"
        stored = await client.vault_request(
            user_id=str(current_user.id),
            brain_id=str(brain.id),
            method="PUT",
            path=f"records/{persisted_record_id}",
            json_body={
                "expected_version": persisted_record_version,
                "title": payload.query[:120],
                "content": f"User: {payload.query}\nAssistant: {answer}",
                "metadata": {
                    "answer": answer,
                    "source": inference.get("source"),
                    "model": inference.get("model"),
                    "tokens_used": inference.get("tokens_used"),
                    "used_records": authorized_records,
                    "interaction_status": "completed",
                    "pending_delete_plans": inference.get(
                        "pending_delete_plans", []
                    ),
                    "delete_result": inference.get("delete_result"),
                    "pending_bulk_replace_plans": inference.get(
                        "pending_bulk_replace_plans", []
                    ),
                    "bulk_replace_result": inference.get("bulk_replace_result"),
                },
            },
        )
        persisted_record_id = str(stored.get("record_id") or persisted_record_id)
    except BrainServiceError as exc:
        if exc.status_code in {401, 403}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Server 2 rejected the Brain {operation}. Check that Server 2 "
                    "is running the current code and reconcile this account's Brain "
                    "and app permissions with POST /api/v1/brain/me/reconcile "
                    f"({exc.error_code}, upstream HTTP {exc.status_code})."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Personal Brain {operation} is unavailable "
                f"({exc.error_code}"
                + (
                    f", upstream HTTP {exc.status_code}"
                    if exc.status_code is not None
                    else ""
                )
                + ")."
            ),
        ) from exc
    return BrainQueryResponse(
        answer=answer,
        request_id=uuid.UUID(request_id),
        source=str(inference.get("source") or "server2_brain"),
        context_used=bool(authorized_records or enriched_context),
        user_id=str(current_user.id),
        model=inference.get("model"),
        latency_ms=int((time.perf_counter() - start) * 1000),
        tokens_used=inference.get("tokens_used"),
        records_used=[
            {
                "id": item["record_id"],
                "source_app": item["source_app"] or "unknown",
                "record_type": item["record_type"] or "record",
                "source_reference": item["source_reference"],
                "content": item["content"],
            }
            for item in authorized_records
            if item.get("record_id")
        ],
        persisted_record_id=(
            uuid.UUID(str(persisted_record_id)) if persisted_record_id else None
        ),
        pending_delete_plans=inference.get("pending_delete_plans", []),
        delete_result=inference.get("delete_result"),
        pending_bulk_replace_plans=inference.get(
            "pending_bulk_replace_plans", []
        ),
        bulk_replace_result=inference.get("bulk_replace_result"),
    )


@router.post("/ingest", response_model=BrainIngestResponse)
async def ingest_to_brain(
    payload: BrainIngestRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainIngestResponse:
    """Send application-generated data to the user's Personal Brain for classification."""
    if payload.source_app:
        authorized = await AppConnectionService(session).has_active_permission(
            current_user, payload.source_app, scope="memory", action="write"
        )
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="App is not authorized to ingest into this user's brain",
            )

    identity, brain = await _vault_brain(session, current_user)
    request_id = str(uuid.uuid4())
    try:
        classification = await get_brain_client().analyze(
            user_id=str(current_user.id),
            content=payload.content,
            context=payload.metadata or {},
            source_app=payload.source_app,
            request_id=request_id,
        )
    except BrainServiceError as exc:
        raise HTTPException(
            status_code=503, detail="Personal Brain analysis is unavailable"
        ) from exc
    namespace = f"apps/{payload.source_app}" if payload.source_app else "shared"
    stored = await _vault_call(
        current_user,
        brain.id,
        method="POST",
        path="records",
        json_body={
            "record_type": payload.content_type[:64],
            "namespace": namespace,
            "idempotency_key": f"ingest:{request_id}",
            "title": str(classification.get("summary") or "")[:512] or None,
            "content": payload.content,
            "source_app": payload.source_app,
            "source_reference": request_id,
            "metadata": {
                **(payload.metadata or {}),
                "analysis_mode": classification.get("analysis_mode"),
                "confidence_kind": classification.get("confidence_kind"),
                "segment_count": len(classification.get("segments") or []),
                "categories": sorted(
                    {
                        str(segment.get("category"))
                        for segment in classification.get("segments", [])
                        if isinstance(segment, dict) and segment.get("category")
                    }
                ),
                "is_original": True,
            },
        },
    )
    record_id = uuid.UUID(str(stored["record_id"]))
    return BrainIngestResponse(
        classification=classification,
        stored=True,
        memory_id=record_id,
        vault_entry_id=record_id,
        record_id=record_id,
    )


@router.post("/memory", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MemoryResponse:
    """Explicitly persist a memory in the authenticated user's Personal Brain."""
    if payload.source_app:
        authorized = await AppConnectionService(session).has_active_permission(
            current_user, payload.source_app, scope="memory", action="write"
        )
        if not authorized:
            raise HTTPException(status_code=403, detail="App is not authorized to write memories")
    identity, brain = await _vault_brain(session, current_user)
    stored = await _vault_call(
        current_user,
        brain.id,
        method="POST",
        path="records",
        json_body={
            "record_type": "personal_memory",
            "namespace": "shared",
            "idempotency_key": str(uuid.uuid4()),
            "title": payload.summary,
            "content": payload.content,
            "source_app": payload.source_app or "brain",
            "metadata": {
                "kind": payload.kind,
                "summary": payload.summary,
                "importance": payload.importance,
                "is_original": True,
            },
        },
    )
    return _memory_response(stored, identity.id)


@router.get("/memory", response_model=list[MemoryResponse])
async def list_memories(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[MemoryResponse]:
    """List persisted memories for the authenticated user."""
    identity, brain = await _vault_brain(session, current_user)
    result = await _vault_call(
        current_user,
        brain.id,
        method="GET",
        path="records",
        params={"namespace": "shared", "record_type": "personal_memory", "limit": limit, "offset": offset},
    )
    return [_memory_response(item, identity.id) for item in result.get("items", [])]


@router.get("/context")
async def brain_context(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Return the current user's brain context in a protected endpoint."""
    _, brain = await _vault_brain(session, current_user)
    return await _vault_call(
        current_user, brain.id, method="GET", path="status"
    )

@router.get("/memory/search", response_model=list[MemoryResponse])
async def search_memories(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[MemoryResponse]:
    """Search persisted memories for the authenticated user."""
    identity, brain = await _vault_brain(session, current_user)
    result = await _vault_call(
        current_user,
        brain.id,
        method="POST",
        path="search",
        json_body={
            "query": q,
            "limit": limit,
            "namespaces": ["shared"],
        },
    )
    return [
        _memory_response(
            {
                "record_id": item["record_id"],
                "content": item.get("content"),
                "source_app": item.get("source_app"),
                "metadata": item.get("metadata") or {},
                "created_at": item.get("created_at"),
            },
            identity.id,
        )
        for item in result.get("results", [])
        if item.get("record_type") == "personal_memory"
    ]


@router.get("/memory/{memory_id}", response_model=MemoryResponse)
async def get_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MemoryResponse:
    """Get a specific memory for the authenticated user."""
    identity, brain = await _vault_brain(session, current_user)
    record = await _vault_call(
        current_user, brain.id, method="GET", path=f"records/{memory_id}"
    )
    if record.get("record_type") != "personal_memory":
        raise HTTPException(status_code=404, detail="Memory not found")
    return _memory_response(record, identity.id)


@router.put("/memory/{memory_id}", response_model=MemoryResponse)
@router.patch("/memory/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MemoryResponse:
    """Update a memory for the authenticated user."""
    identity, brain = await _vault_brain(session, current_user)
    current = await _vault_call(
        current_user, brain.id, method="GET", path=f"records/{memory_id}"
    )
    if current.get("record_type") != "personal_memory":
        raise HTTPException(status_code=404, detail="Memory not found")
    metadata = dict(current.get("metadata") or {})
    if payload.importance is not None:
        metadata["importance"] = payload.importance
    result = await _vault_call(
        current_user,
        brain.id,
        method="PUT",
        path=f"records/{memory_id}",
        json_body={
            "expected_version": current["version"],
            "title": payload.summary,
            "summary": payload.summary,
            "metadata": metadata,
            "content": payload.content,
            "confirm_original_edit": payload.confirm_original_edit,
        },
    )
    return _memory_response(result, identity.id)


@router.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID,
    confirm: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Delete a memory for the authenticated user."""
    if not confirm:
        raise HTTPException(status_code=409, detail="Explicit delete confirmation is required")
    _, brain = await _vault_brain(session, current_user)
    await _vault_call(
        current_user,
        brain.id,
        method="DELETE",
        path=f"records/{memory_id}",
        params={"confirm": "true"},
    )
    return None
