from fastapi import APIRouter

from src.core.config import settings
from src.schemas.response_meta import StoryResponseMeta
from src.schemas.story import StorylinesRequest, StorylinesResponse
from src.schemas.story_compile import StoryCompileRequest, StoryCompileResponse
from src.services import story_llm
from src.services.prompt import STORYLINES_VERSION, build_storylines_prompt

router = APIRouter()


@router.post("/story/storylines", response_model=StorylinesResponse)
async def generate_storylines(request: StorylinesRequest) -> StorylinesResponse:
    system_prompt, user_prompt = build_storylines_prompt(
        request.genre_tags,
        request.protagonist_tags,
        request.supporting_tags,
    )
    result, usage = await story_llm.generate_storylines(system_prompt, user_prompt)
    meta = StoryResponseMeta(
        model=usage.model,
        prompt_versions={"STORYLINES": STORYLINES_VERSION},
        provider=settings.llm_provider,
        input_token_count=usage.input_tokens,
        output_token_count=usage.output_tokens,
        retry_count=0,  # storylines는 단일 호출 — 재호출 없음
    )
    # LLM 원시 dict를 splat하지 않고 stories만 명시적으로 꺼낸다 — result에 'meta' 키가
    # 섞여 와도 meta= 인자와 kwarg 충돌(500)이 나지 않게 한다.
    return StorylinesResponse(stories=result["stories"], meta=meta)


@router.post("/story/compile", response_model=StoryCompileResponse)
async def create_story_compile(request: StoryCompileRequest) -> StoryCompileResponse:
    """시점 A-1: 희소 입력(선택 스토리라인 + 추가정보 + 태그)을 스토리 명세로
    컴파일해 ERD 4테이블 nested 계약으로 반환한다. 검증·재호출·502 변환은
    compile_story()가 모두 처리한다."""
    return await story_llm.compile_story(request)
