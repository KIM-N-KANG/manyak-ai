from fastapi import APIRouter

from src.schemas.moderation import StoryModerationRequest, StoryModerationResponse
from src.services.moderation import service

router = APIRouter(prefix="/moderation")


@router.post("/story", response_model=StoryModerationResponse)
async def moderate_story(request: StoryModerationRequest) -> StoryModerationResponse:
    result = await service.moderate_story(request.model_dump(by_alias=True, exclude_none=True))
    return StoryModerationResponse(**result.model_dump())
