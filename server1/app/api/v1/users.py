"""User API routes for user management."""

from __future__ import annotations

import asyncio
import hmac
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.schemas.user import UserCreate, UserResponse
from app.schemas.brain import BrainProvisioningResponse
from app.services.app_registry import AppConnectionService
from app.services.user import DuplicateEmailError, UserCreationError, UserService

logger = get_logger(__name__)
router = APIRouter()


async def _sync_brain_app_connections(session: AsyncSession, user: User) -> None:
    """Refresh Server 2's app-consent snapshot from Server 1's source of truth."""
    identity = await AIIdentityRepository(session).get_by_user_id(user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The user's Brain must be ready before app permissions can sync.",
        )

    client = get_brain_client()
    connections = await AppConnectionService(session).list_connections(user)
    app_slugs = {connection.module_slug for connection in connections}
    try:
        remote = await asyncio.wait_for(
            client.brain_app_request(
                user_id=str(user.id),
                brain_id=str(brain.id),
                method="GET",
            ),
            timeout=5.0,
        )
        for connection in connections:
            await asyncio.wait_for(
                client.brain_app_request(
                    user_id=str(user.id),
                    brain_id=str(brain.id),
                    method="PUT",
                    json_body={
                        "app_slug": connection.module_slug,
                        "is_active": connection.is_active,
                        "consent_state": (
                            "granted" if connection.is_active else "suspended"
                        ),
                        "granted_scopes": [
                            {
                                "scope": permission.scope,
                                "action": permission.permission.value,
                            }
                            for permission in connection.module_permissions
                        ],
                    },
                ),
                timeout=5.0,
            )

        for remote_connection in remote.get("items", []):
            app_slug = remote_connection.get("app_slug")
            if isinstance(app_slug, str) and app_slug not in app_slugs:
                await asyncio.wait_for(
                    client.brain_app_request(
                        user_id=str(user.id),
                        brain_id=str(brain.id),
                        method="DELETE",
                        app_slug=app_slug,
                    ),
                    timeout=5.0,
                )
    except (BrainServiceError, TimeoutError) as exc:
        logger.warning(
            "Brain app-permission sync failed for user %s (%s)",
            user.id,
            getattr(exc, "error_code", type(exc).__name__),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not synchronize the user's app permissions with their Brain.",
        ) from exc


async def _provision_primary_brain(session: AsyncSession, user: User) -> None:
    """Synchronize the committed canonical Brain to Server 2, retaining retry state."""
    if not get_settings().brain_remote_provisioning_enabled:
        return
    identity = await AIIdentityRepository(session).get_by_user_id(user.id)
    if identity is None:
        logger.error("Cannot provision Brain for user %s: identity missing", user.id)
        return
    brains = PersonalAIBrainRepository(session)
    brain = await brains.get_by_ai_identity_id(identity.id)
    if brain is None:
        logger.error("Cannot provision Brain for user %s: registry row missing", user.id)
        return

    try:
        configured_model = brain.model_configuration.get("model_id", "deployment_default")
        if configured_model == "deployment_default":
            configured_model = get_settings().ai_server_model
        raw_result = await asyncio.wait_for(
            get_brain_client().provision_brain(
                brain_id=str(brain.id),
                owner_id=str(user.id),
                ai_identity_id=str(identity.id),
                owner_name=identity.display_name or user.full_name,
                model_id=configured_model,
                max_concurrent_requests=int(
                    brain.compute_allocation.get("max_concurrent_requests", 1)
                ),
            ),
            timeout=get_settings().brain_provisioning_timeout_seconds,
        )
    except BrainServiceError as exc:
        brain.provisioning_status = "failed" if exc.status_code == 409 else "pending"
        brain.inference_runtime_status = "unavailable"
        brain.provisioning_error = exc.error_code
        await session.commit()
        logger.warning(
            "Brain provisioning for user %s did not complete (%s)",
            user.id,
            exc.error_code,
        )
        return
    except TimeoutError:
        brain.provisioning_status = "pending"
        brain.inference_runtime_status = "unavailable"
        brain.provisioning_error = "provisioning_timeout"
        await session.commit()
        logger.warning("Brain provisioning timed out for user %s", user.id)
        return
    try:
        result = BrainProvisioningResponse.model_validate(raw_result)
    except ValidationError:
        brain.provisioning_status = "failed"
        brain.inference_runtime_status = "unavailable"
        brain.provisioning_error = "invalid_provisioning_response"
        await session.commit()
        logger.error("Server 2 returned invalid provisioning metadata for %s", brain.id)
        return

    brain.provisioning_status = result.provisioning_status
    brain.inference_runtime_status = result.inference_runtime_status
    brain.resource_manifest_version = result.resource_manifest_version
    brain.model_configuration = result.model_configuration
    brain.compute_allocation = result.compute_allocation
    brain.storage_resource_reference = result.storage_resource_reference
    brain.last_health_check = result.last_health_check
    brain.llm_runtime_id = result.llm_runtime_id
    brain.runtime_isolation = result.runtime_isolation
    brain.provisioning_error = None
    await session.commit()


def _registration_allowed(bootstrap_token: str | None) -> bool:
    """Return whether account creation is permitted for this request.

    When ``ALLOW_PUBLIC_REGISTRATION`` is enabled (the default) anyone may
    register. When it is disabled, only a request that presents the configured
    ``BOOTSTRAP_TOKEN`` may create the initial account(s); this closes public
    sign-up without needing an admin role that does not exist yet.
    """
    settings = get_settings()
    if settings.allow_public_registration:
        return True
    expected = settings.bootstrap_token
    if not expected or not bootstrap_token:
        return False
    return hmac.compare_digest(bootstrap_token, expected)


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Users"],
)
async def create_user(
    data: UserCreate,
    session: AsyncSession = Depends(get_db),
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
) -> UserResponse:
    """Create a new user account with a default identity and Personal Brain."""
    if not _registration_allowed(x_bootstrap_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled",
        )

    service = UserService(session)

    if await service.user_email_exists(data.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    try:
        user = await service.create_user(
            email=data.email,
            password_hash=hash_password(data.password),
            full_name=data.full_name,
        )
    except DuplicateEmailError as exc:
        # Race between the pre-check and the insert.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        ) from exc
    except UserCreationError as exc:
        # Details are logged server-side by the service; clients get a safe message.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create account",
        ) from exc

    logger.info("Registered new user account %s", user.id)
    if get_settings().brain_remote_provisioning_enabled:
        await AppConnectionService(session).install(user, "aura-core")
    await _provision_primary_brain(session, user)
    return user


@router.get(
    "/users/{user_id}",
    response_model=UserResponse,
    tags=["Users"],
)
async def get_user(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve the authenticated user's profile by ID."""
    if current_user.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's record")

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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve the authenticated user by email address."""
    if current_user.email.lower() != email.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's account")

    service = UserService(session)
    user = await service.get_user_by_email(email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user
