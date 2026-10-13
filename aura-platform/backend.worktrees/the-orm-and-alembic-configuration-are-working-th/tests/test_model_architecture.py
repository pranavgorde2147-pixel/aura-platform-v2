from app.models.ai_identity import AIIdentity
from app.models.device import Device
from app.models.installed_module import InstalledModule
from app.models.knowledge_graph import KnowledgeGraph
from app.models.memory_vault import MemoryVault
from app.models.module_permission import ModulePermission
from app.models.personal_ai_brain import PersonalAIBrain
from app.models.session import Session
from app.models.timeline import Timeline
from app.models.user import User


def test_core_models_expose_expected_architecture_fields():
    assert hasattr(PersonalAIBrain, "version")
    assert hasattr(PersonalAIBrain, "state")
    assert hasattr(PersonalAIBrain, "sync_version")
    assert hasattr(PersonalAIBrain, "last_sync_at")

    assert hasattr(MemoryVault, "object_reference")
    assert hasattr(MemoryVault, "storage_type")
    assert hasattr(MemoryVault, "processing_status")

    assert hasattr(KnowledgeGraph, "graph_type")
    assert hasattr(KnowledgeGraph, "graph_metadata")
    assert hasattr(KnowledgeGraph, "node_count")

    assert hasattr(Timeline, "resource_type")
    assert hasattr(Timeline, "resource_reference")
    assert hasattr(Timeline, "occurred_at")

    assert hasattr(Device, "platform")
    assert hasattr(Device, "app_version")
    assert hasattr(Device, "push_token")
    assert hasattr(Device, "last_seen_at")
    assert hasattr(Device, "trusted")
    assert hasattr(Device, "revoked")

    assert hasattr(Session, "refresh_token_hash")
    assert hasattr(Session, "expires_at")
    assert hasattr(Session, "revoked_at")
    assert hasattr(Session, "is_revoked")
    assert hasattr(Session, "device_id")

    assert hasattr(InstalledModule, "module_name")
    assert hasattr(InstalledModule, "is_active")

    assert hasattr(ModulePermission, "scope")

    assert hasattr(User, "email")
    assert hasattr(AIIdentity, "ai_uuid")
