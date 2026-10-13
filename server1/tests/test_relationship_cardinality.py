from app.models.ai_identity import AIIdentity
from app.models.personal_ai_brain import PersonalAIBrain
from app.models.user import User


def test_user_has_single_ai_identity_and_brain():
    assert User.__table__.name == "users"
    assert AIIdentity.__table__.name == "ai_identities"
    assert PersonalAIBrain.__table__.name == "personal_ai_brains"

    user_relationships = {name for name in User.__dict__ if name.startswith("ai") or name.startswith("personal")}
    assert "ai_identity" in user_relationships
    assert "ai_identities" not in user_relationships

    ai_relationships = {name for name in AIIdentity.__dict__ if name.startswith("personal") or name.startswith("user")}
    assert "personal_ai_brain" in ai_relationships or "personal_ai_brains" in ai_relationships
