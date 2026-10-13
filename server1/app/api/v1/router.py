"""Central router for the versioned API surface."""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.apps import router as apps_router
from app.api.v1.brain import router as brain_router
from app.api.v1.files import router as files_router
from app.api.v1.health import router as health_router
from app.api.v1.users import router as users_router
from app.api.v1.identity import router as identity_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(brain_router)
api_router.include_router(identity_router)
api_router.include_router(files_router)
api_router.include_router(apps_router)

