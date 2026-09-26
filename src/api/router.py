from fastapi import APIRouter

from src.api.v1 import chat, health, story
from src.api.v1.moderation import story as moderation_story

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(story.router, tags=["story"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(moderation_story.router, tags=["moderation"])
