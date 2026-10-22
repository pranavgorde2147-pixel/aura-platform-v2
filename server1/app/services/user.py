"""User service layer for user management."""

from __future__ import annotations

import uuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.ai_identity import AIIdentity
from app.repositories.user import UserRepository
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class DuplicateEmailError(Exception):
    """Raised when an account cannot be created because the email exists."""

    def __init__(self, email: str):
        super().__init__("Email already registered")
        self.email = email


class UserCreationError(Exception):
    """Raised when account provisioning fails and the transaction is rolled back."""


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return True when an integrity error is a unique-constraint violation."""
    orig = getattr(exc, "orig", None)
    if getattr(orig, "sqlstate", None) == "23505":
        return True
    return "unique" in str(exc).lower()


class UserService:
    """Service for managing users and their identities."""

    def __init__(self, session: AsyncSession):
        """Initialize the service with dependencies."""
        self.session = session
        self.user_repo = UserRepository(session)
        self.identity_repo = AIIdentityRepository(session)
        self.brain_repo = PersonalAIBrainRepository(session)

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str | None = None,
    ) -> User:
        """Create a user with exactly one default identity and Personal Brain.

        The user, its default AI identity and the Personal Brain binding are
        created in a single transaction. If any step fails the whole transaction
        is rolled back so a failed registration can never leave a half-created
        account behind (for example a user with no brain, which would silently
        break that account's queries later).
        """
        try:
            user = await self.user_repo.create(
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                is_active=True,
            )
            identity = await self.identity_repo.create(
                user_id=user.id,
                display_name=full_name or f"AURA-{user.email.split('@', 1)[0]}",
                is_default=True,
            )
            brain = await self.brain_repo.create(
                ai_identity_id=identity.id,
                brain_name=full_name or "Personal Brain",
                version="1.0",
                state="initializing",
                provisioning_status="pending",
                resource_manifest_version=1,
                model_configuration={"model_id": get_settings().ai_server_model},
                inference_runtime_status="unregistered",
                compute_allocation={"max_concurrent_requests": 1},
                storage_resource_reference=f"brain:{identity.id}",
            )
            # Single commit for the whole account graph.
            await self.user_repo.commit()
        except IntegrityError as exc:
            await self.user_repo.rollback()
            if _is_unique_violation(exc):
                # Duplicate email is the expected, safe race outcome.
                raise DuplicateEmailError(email) from exc
            logger.error("Account provisioning failed: database integrity error")
            raise UserCreationError("Account could not be created") from exc
        except Exception as exc:
            await self.user_repo.rollback()
            # Log only the exception class; never the email or SQL parameters.
            logger.error("Account provisioning failed: %s", type(exc).__name__)
            raise UserCreationError("Account could not be created") from exc

        logger.info(
            "Provisioned account %s with default identity %s and Personal Brain %s",
            user.id,
            identity.id,
            brain.id,
        )
        return user

    async def get_user_by_email(self, email: str) -> User | None:
        """Retrieve a user by email."""
        return await self.user_repo.get_by_email(email)

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        """Retrieve a user by ID."""
        return await self.user_repo.get_by_id(user_id)

    async def get_user_with_identity(self, user_id: uuid.UUID) -> tuple[User | None, AIIdentity | None]:
        """Retrieve a user with their AI identity."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            return None, None
        
        identity = await self.identity_repo.get_by_user_id(user_id)
        return user, identity

    async def user_email_exists(self, email: str) -> bool:
        """Check if a user with the given email exists."""
        return await self.user_repo.email_exists(email)

    async def update_user(self, user_id: uuid.UUID, **kwargs) -> User | None:
        """Update user information."""
        # Remove sensitive fields from being updated directly
        kwargs.pop("id", None)
        kwargs.pop("password_hash", None)
        
        user = await self.user_repo.update(user_id, **kwargs)
        if user:
            await self.user_repo.commit()
        return user

    async def deactivate_user(self, user_id: uuid.UUID) -> User | None:
        """Deactivate a user account."""
        user = await self.user_repo.update(user_id, is_active=False)
        if user:
            await self.user_repo.commit()
        logger.info(f"Deactivated user {user_id}")
        return user
