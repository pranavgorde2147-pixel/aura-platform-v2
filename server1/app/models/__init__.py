"""Domain models package for the AURA backend.

Every ORM model is imported here so the SQLAlchemy mapper registry always
contains the complete model graph. Relationship targets are resolved by class
name, so a model that is used without its related models being imported first
would fail to configure. Importing the whole package keeps that impossible.
"""

from app.models import (  # noqa: F401
    ai_identity,
    app_record,
    base,
    brain_memory,
    brain_processing,
    device,
    installed_module,
    knowledge_graph,
    memory_vault,
    module_permission,
    personal_ai_brain,
    session,
    timeline,
    user,
)

__all__ = [
    "ai_identity",
    "app_record",
    "base",
    "brain_memory",
    "brain_processing",
    "device",
    "installed_module",
    "knowledge_graph",
    "memory_vault",
    "module_permission",
    "personal_ai_brain",
    "session",
    "timeline",
    "user",
]
