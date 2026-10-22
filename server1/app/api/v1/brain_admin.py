"""Allowlisted, metadata-only administration for per-user Brain resources."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.ai_identity import AIIdentity
from app.models.app_record import AppRecord
from app.models.brain_processing import BrainProcessingRun
from app.models.installed_module import InstalledModule
from app.models.personal_ai_brain import PersonalAIBrain
from app.models.user import User
from app.schemas.brain_admin import BrainAdminItem, BrainAdminListResponse

router = APIRouter(prefix="/admin/brains", tags=["Brain Administration"])


def _require_brain_admin(user: User) -> None:
    configured = get_settings().brain_admin_user_ids
    try:
        allowed = {str(uuid.UUID(item.strip())) for item in configured.split(",") if item.strip()}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Brain administration is not configured correctly",
        ) from exc
    if str(user.id) not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brain administrator authorization required",
        )


@router.get("", response_model=BrainAdminListResponse)
async def list_brains(
    provisioning_status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainAdminListResponse:
    """List Brain resource metadata without exposing private Brain content."""
    _require_brain_admin(current_user)
    counts_result = await session.execute(
        select(
            PersonalAIBrain.provisioning_status,
            func.count(PersonalAIBrain.id),
        ).group_by(PersonalAIBrain.provisioning_status)
    )
    counts = {str(state): count for state, count in counts_result.all()}

    query = (
        select(PersonalAIBrain, AIIdentity.user_id)
        .join(AIIdentity, PersonalAIBrain.ai_identity_id == AIIdentity.id)
        .order_by(PersonalAIBrain.created_at.desc())
    )
    if provisioning_status:
        query = query.where(PersonalAIBrain.provisioning_status == provisioning_status)
    total_query = select(func.count()).select_from(query.subquery())
    total = int((await session.scalar(total_query)) or 0)
    result = await session.execute(query.limit(limit).offset(offset))

    items: list[BrainAdminItem] = []
    for brain, owner_id in result.all():
        identity_id = brain.ai_identity_id
        relational_bytes = int(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(func.length(AppRecord.content)), 0)).where(
                        AppRecord.user_id == owner_id,
                        AppRecord.content.is_not(None),
                    )
                )
            )
            or 0
        )
        app_count = int(
            (
                await session.scalar(
                    select(func.count(InstalledModule.id)).where(
                        InstalledModule.user_id == owner_id,
                        InstalledModule.is_active.is_(True),
                    )
                )
            )
            or 0
        )
        recent_status = await session.scalar(
            select(BrainProcessingRun.status)
            .where(BrainProcessingRun.user_id == owner_id)
            .order_by(BrainProcessingRun.created_at.desc())
            .limit(1)
        )
        items.append(
            BrainAdminItem(
                brain_id=brain.id,
                owner_user_id=owner_id,
                ai_identity_id=identity_id,
                provisioning_status=brain.provisioning_status,
                created_at=brain.created_at,
                updated_at=brain.updated_at,
                inference_runtime_status=brain.inference_runtime_status,
                model_configuration_id=str(
                    brain.model_configuration.get("model_id", "unknown")
                ),
                compute_allocation=brain.compute_allocation,
                inference_request_count=brain.inference_request_count,
                storage_resource_reference=brain.storage_resource_reference,
                storage_bytes_relational=relational_bytes,
                connected_app_count=app_count,
                memory_retrieval_status="database_status_unverified",
                recent_processing_job_status=(
                    recent_status.value if hasattr(recent_status, "value") else recent_status
                ),
                last_health_check=brain.last_health_check,
                provisioning_error=brain.provisioning_error,
                llm_runtime_id=brain.llm_runtime_id,
                runtime_isolation=brain.runtime_isolation,
            )
        )
    return BrainAdminListResponse(
        total=total,
        provisioning_status_counts=counts,
        limit=limit,
        offset=offset,
        items=items,
    )


@router.post("/reconcile")
async def reconcile_pending_brains(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Retry a bounded batch of pending/failed Brain registrations."""
    _require_brain_admin(current_user)
    result = await session.execute(
        select(User)
        .join(AIIdentity, AIIdentity.user_id == User.id)
        .join(PersonalAIBrain, PersonalAIBrain.ai_identity_id == AIIdentity.id)
        .where(PersonalAIBrain.provisioning_status != "ready")
        .order_by(PersonalAIBrain.created_at.asc())
        .limit(limit)
    )
    users = list(result.scalars().all())
    from app.api.v1.users import _provision_primary_brain

    for user in users:
        await _provision_primary_brain(session, user)
    ready = 0
    for user in users:
        state = await session.scalar(
            select(PersonalAIBrain.provisioning_status)
            .join(AIIdentity, PersonalAIBrain.ai_identity_id == AIIdentity.id)
            .where(AIIdentity.user_id == user.id)
        )
        ready += state == "ready"
    return {
        "processed": len(users),
        "ready": ready,
        "pending_or_failed": len(users) - ready,
    }
