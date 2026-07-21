from fastapi import APIRouter

from src.core.config import settings
from src.core.langfuse import dimension_tags, observe_request
from src.schemas.response_meta import StoryResponseMeta
from src.schemas.story import StorylinesRequest, StorylinesResponse
from src.schemas.story_compile import StoryCompileRequest, StoryCompileResponse
from src.services import story_llm
from src.services.prompt import COMPILE_VERSION, STORYLINES_VERSION, build_storylines_prompt

router = APIRouter()


@router.post("/story/storylines", response_model=StorylinesResponse)
async def generate_storylines(request: StorylinesRequest) -> StorylinesResponse:
    # 요청 1건 = 트레이스 1건(KNK-624). 장르·태그·프롬프트 버전을 분석 차원으로 싣는다(KNK-640).
    with observe_request(
        "스토리라인 생성",
        # 장르만 태그로 — 주인공·조연 태그는 사용자 자유입력이 섞여 제외(dimension_tags 참조).
        tags=dimension_tags(genre_tags=request.genre_tags),
        metadata={
            "prompt_versions": {"STORYLINES": STORYLINES_VERSION},
            "retry_count": 0,  # storylines는 단일 호출 — 재호출 없음
        },
    ):
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
    # 부분 재호출(최대 3회)까지 한 트레이스로 묶인다(KNK-624). 분석 차원 부착(KNK-640):
    # 장르·태그·프롬프트 버전은 미리, 재호출 횟수는 응답 meta에서 사후에 싣는다.
    with observe_request(
        "스토리 컴파일",
        tags=dimension_tags(genre_tags=request.genre_tags),  # 장르만(위와 동일 이유)
        metadata={"prompt_versions": {"COMPILE": COMPILE_VERSION}},
    ) as trace:
        response = await story_llm.compile_story(request)
        # meta는 항상 채워지지만(compile_story 계약), 관측이 서비스를 깨지 않도록 None을 방어한다.
        if response.meta is not None:
            trace.set_metadata(retry_count=response.meta.retry_count)
        return response
