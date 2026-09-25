from fastapi import APIRouter

from src.core.langfuse import observe_request
from src.schemas.moderation import StoryModerationRequest, StoryModerationResponse
from src.services.moderation import service
from src.services.moderation.models import ModerationCall
from src.services.moderation.prompt import MODERATION_VERSION
from src.services.moderation.observation import without_media

router = APIRouter(prefix="/moderation")


@router.post("/story", response_model=StoryModerationResponse)
async def moderate_story(request: StoryModerationRequest) -> StoryModerationResponse:
    post = request.model_dump(by_alias=True, exclude_none=True)
    calls: list[ModerationCall] = []
    with observe_request(
        "게시물 검수",
        input_data=without_media(post),
        metadata={"story_id": without_media(post["storyId"]), "prompt_versions": {"MODERATION": MODERATION_VERSION}},
    ) as trace:
        try:
            result = await service.moderate_story(post, calls=calls)
            response = StoryModerationResponse(**result.model_dump())
            trace.set_output(without_media(response.model_dump(mode="json")))
            return response
        finally:
            trace.set_metadata(calls=without_media([call.model_dump(mode="json") for call in calls]))
