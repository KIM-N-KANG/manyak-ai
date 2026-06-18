from fastapi import APIRouter

from src.schemas.story import StorylinesRequest, StorylinesResponse
from src.schemas.story_compile import StoryCompileRequest, StoryCompileResponse
from src.services.story_llm import compile_story, generate_storylines
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


@router.post("/story/compile", response_model=StoryCompileResponse)
async def create_story_compile(request: StoryCompileRequest) -> StoryCompileResponse:
    """시점 A-1: 희소 입력(선택 스토리라인 + 추가정보 + 태그)을 스토리 명세로
    컴파일해 ERD 4테이블 nested 계약으로 반환한다. 검증·재호출·502 변환은
    compile_story()가 모두 처리한다."""
    return await compile_story(request)
