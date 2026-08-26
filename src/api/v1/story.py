from fastapi import APIRouter, HTTPException

from src.core.langfuse import dimension_tags, observe_request
from src.core.request_context import select_connection_metadata
from src.schemas.response_meta import StoryResponseMeta
from src.schemas.story import StorylinesRequest, StorylinesResponse
from src.schemas.story_compile import StoryCompileRequest, StoryCompileResponse
from src.core.config import settings
from src.services import story_llm
from src.services.image.prompt import CHARACTER_IMAGE_VERSION
from src.services.llm import provider_of
from src.services.llm.base import PROVIDER_GOOGLE
from src.services.prompt import (
    COMPILE_GEMINI_VERSION,
    COMPILE_VERSION,
    STORYLINES_VERSION,
    build_storylines_prompt,
)

router = APIRouter()


@router.post("/story/storylines", response_model=StorylinesResponse)
async def generate_storylines(request: StorylinesRequest) -> StorylinesResponse:
    # 요청 1건 = 트레이스 1건(KNK-624). 장르·태그·프롬프트 버전을 분석 차원으로 싣는다(KNK-640).
    with observe_request(
        "스토리라인 생성",
        input_data=request.model_dump(mode="json"),
        # 장르만 태그로 — 인물 세트 특징(features)은 사용자 자유입력이 섞여 제외(dimension_tags 참조).
        tags=dimension_tags(genre_tags=request.genre_tags),
        metadata={
            **select_connection_metadata("creation_id", "parent_creation_id"),
            "prompt_versions": {"STORYLINES": STORYLINES_VERSION},
            # 기본값 0을 미리 싣는다 — 예외로 조기 이탈해도 실패 트레이스에 값이 비지 않게(KNK-312 리뷰 F2).
            "retry_count": 0,
        },
    ) as trace:
        system_prompt, user_prompt = build_storylines_prompt(
            request.genre_tags,
            request.protagonist,
            request.supporting_characters,
        )
        try:
            result, usage = await story_llm.generate_storylines(
                system_prompt,
                user_prompt,
                # 사용자가 이름 지은 주변 인물은 세 편 모두 등장해야 한다(KNK-833).
                required_names=[c.name for c in request.supporting_characters if c.name],
            )
        except HTTPException as e:
            # 실패한 요청도 실제 재호출 횟수를 기록한다 — 502 예외에 실려 온다(story_llm).
            trace.set_metadata(retry_count=getattr(e, "retry_count", 0))
            raise
        # 재호출 횟수는 호출 결과에서만 알 수 있어 사후에 싣는다(compile과 동일 패턴, KNK-312).
        trace.set_metadata(retry_count=usage.retry_count)
        meta = StoryResponseMeta(
            model=usage.model,
            prompt_versions={"STORYLINES": STORYLINES_VERSION},
            provider=usage.provider,
            input_token_count=usage.input_tokens,
            output_token_count=usage.output_tokens,
            retry_count=usage.retry_count,  # invalid 응답 재호출 횟수(0~2, KNK-312)
        )
        # LLM 원시 dict를 splat하지 않고 stories만 명시적으로 꺼낸다 — result에 'meta' 키가
        # 섞여 와도 meta= 인자와 kwarg 충돌(500)이 나지 않게 한다.
        return StorylinesResponse(stories=result["stories"], meta=meta)


@router.post("/story/compile", response_model=StoryCompileResponse)
async def create_story_compile(request: StoryCompileRequest) -> StoryCompileResponse:
    """시점 A-1: 희소 입력(선택 스토리라인 + 추가정보 + 장르 태그 + 인물 세트)을 스토리 명세로
    컴파일해 ERD 4테이블 nested 계약으로 반환한다. 검증·재호출·502 변환은
    compile_story()가 모두 처리한다."""
    # 부분 재호출(최대 3회)까지 한 트레이스로 묶인다(KNK-624). 분석 차원 부착(KNK-640):
    # 장르·태그·프롬프트 버전은 미리 싣고, 재호출 횟수는 응답 meta에서 사후에 싣는다.
    # prompt_versions는 호출 전에 넣어야 실패해도 기록된다(Codex 리뷰 P2).
    compile_provider = provider_of(settings.story_compile_model)
    if compile_provider == PROVIDER_GOOGLE:
        _pv = {
            "COMPILE_GEMINI": COMPILE_GEMINI_VERSION,
            "CHARACTER_IMAGE": CHARACTER_IMAGE_VERSION,
        }
    else:
        _pv = {
            "COMPILE": COMPILE_VERSION,
            "CHARACTER_IMAGE": CHARACTER_IMAGE_VERSION,
        }
    with observe_request(
        "스토리 컴파일",
        input_data=request.model_dump(mode="json"),
        tags=dimension_tags(genre_tags=request.genre_tags),  # 장르만(위와 동일 이유)
        metadata={
            **select_connection_metadata("creation_id", "storyline_id", "storyline_order"),
            "prompt_versions": _pv,
        },
    ) as trace:
        response = await story_llm.compile_story(request)
        # meta는 항상 채워지지만(compile_story 계약), 관측이 서비스를 깨지 않도록 None을 방어한다.
        if response.meta is not None:
            trace.set_metadata(
                retry_count=response.meta.retry_count,
                prompt_versions=response.meta.prompt_versions,
            )
        return response
