from fastapi import APIRouter

from src.api.v1 import health, story

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(story.router, tags=["story"])
