from fastapi import APIRouter

from src.schemas.story import StorylinesRequest, StorylinesResponse
from src.services.gemini import generate_storylines
from src.services.prompt import build_story_prompt

router = APIRouter()


@router.post("/story/storylines", response_model=StorylinesResponse)
async def create_storylines(request: StorylinesRequest) -> StorylinesResponse:
    system_prompt, user_prompt = build_story_prompt(
        request.genre_tags,
        request.protagonist_tags,
        request.supporting_tags,
    )
    result = await generate_storylines(system_prompt, user_prompt)
    return StorylinesResponse(**result)
