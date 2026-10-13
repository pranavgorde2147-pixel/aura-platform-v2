"""Versioned API router for Server 3."""

from fastapi import APIRouter

from app.api import storage_health as health, objects

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(objects.router)
