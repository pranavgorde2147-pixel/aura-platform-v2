"""Versioned API router for Server 3."""

from fastapi import APIRouter

from app.api import health, objects

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(objects.router)
