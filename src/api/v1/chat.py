"""채팅 API (시점 B) — 턴 SSE 스트리밍 + 선택지 동기 엔드포인트.

`POST /api/v1/chat/turns`: ChatTurnRequest를 받아 6레이어 프롬프트를 조립하고
(`assemble`), 본문을 스트리밍 호출해(`stream_chat_turn`) 토큰을 흘린다. 본문 스트림이
끝나면 사건·엔딩 판정 호출(`generate_judgement` — 재료 없으면 스킵, 실패하면 null)만
실행해 본문·판정 메타·합산 meta를 completed 이벤트로 발행한다. **completed가 선택지를
기다리지 않는다** — 선택지는 전용 엔드포인트로 분리됐다(KNK-625, 지연 해소).

`POST /api/v1/chat/choices`: 분리된 선택지 생성(동기 REST). 백엔드가 completed 이후
같은 재료 + 방금 본문(ai_output)으로 호출한다. 항상 200 + 정확히 3개(폴백 흡수).

AI가 발행하는 SSE 이벤트는 token·completed·error 3개뿐이다(명세 B). started·chatId·turnId는
백엔드(manyak-server)가 부착한다. completed의 ai_output·meta는 와이어 계약 키(aiOutput·
camelCase)로 직렬화한다(by_alias=True). completed의 choices는 하위호환 빈 배열 고정 —
백엔드는 '빈 배열이면 저장하지 않음'(4-backend §4-3-3)이라 선행 배포에 안전하다.
판정 메타 3필드(targetMainEvent·occurredMainEventName·endingName)는 재료 없는 요청에서
null이고, 서버 DTO가 ignoreUnknown이라 역시 선행 배포에 안전하다.
"""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.core.config import settings
from src.core.langfuse import observe_request
from src.schemas.chat_choices import ChatChoicesRequest, ChatChoicesResponse
from src.schemas.chat_turn import (
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_TOKEN,
    ChatTurnRequest,
    CompletedData,
    ErrorData,
    TokenData,
)
from src.schemas.response_meta import ChatResponseMeta, StoryResponseMeta
from src.services import llm
from src.services.chat_assembler import LAYER_VERSIONS, assemble
from src.services.chat_llm import stream_chat_turn
from src.services.chat_choices import NEXT_ACTIONS_VERSION, generate_choices
from src.services.chat_judgement import JUDGEMENT_VERSION, generate_judgement

router = APIRouter()


def _sse(event: str, data: dict) -> str:
    """SSE 한 프레임으로 직렬화한다(`event:`/`data:` 한 줄씩 + 빈 줄로 종료)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _add_tokens(a: int | None, b: int | None) -> int | None:
    """본문·판정 두 호출의 토큰을 합산한다. 둘 다 None이면 None, 아니면 누락을 0으로 본다."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


async def _event_stream(req: ChatTurnRequest) -> AsyncIterator[str]:
    """조립 → 본문 스트리밍 → (종료 후) 판정 호출 → 합쳐 SSE 프레임으로 낸다.

    본문 스트림이 error로 끝나면 그 error만 relay하고 판정 호출은 하지 않는다.

    트레이스(KNK-624)는 핸들러가 아니라 **이 제너레이터 안에서** 연다 — SSE는 응답이 200으로
    열린 뒤에 본 작업이 돌기 때문에, 핸들러가 반환하는 시점에는 아직 LLM 호출 전이다. 블록이
    스트림 전체를 감싸므로 본문·판정 두 호출이 한 트레이스에 묶인다.
    """
    # 분석 차원 부착(KNK-640): 6레이어+판정 버전을 싣는다. 채팅 턴은 재호출이 없어 retry_count=0.
    # 장르 태그는 스토리 제작 트레이스에만 — 채팅 쪽은 KNK-652에서 제거(5-ai-server §5-6).
    with observe_request(
        "채팅 턴",
        metadata={
            "prompt_versions": {**LAYER_VERSIONS, "JUDGEMENT": JUDGEMENT_VERSION},
            "retry_count": 0,
        },
    ):
        messages = assemble(req)
        async for ev in stream_chat_turn(messages):
            name = ev["event"]
            if name == EVENT_TOKEN:
                yield _sse(name, TokenData(text=ev["text"]).model_dump())
            elif name == EVENT_COMPLETED:
                ai_output = ev["ai_output"]
                # 본문이 끝난 뒤 판정만 실행한다 — 선택지는 전용 엔드포인트(/chat/choices)로
                # 분리됐다(KNK-625). completed가 선택지 생성을 기다리지 않아 본문 확정이
                # 밀리지 않는다. 판정은 재료 없으면 스킵·실패하면 null(턴을 깨지 않음).
                judgement = await generate_judgement(req, ai_output)
                # 메타 합산: 토큰은 본문+판정, prompt_versions는 6레이어+JUDGEMENT.
                # retry_count는 0 고정 — 본문·판정은 재호출이 없고, 선택지 재호출 횟수는
                # /chat/choices 응답 meta로 이동했다(NEXT_ACTIONS 버전 키도 함께 이동).
                meta = ChatResponseMeta(
                    model=ev.get("model") or settings.chat_model,
                    prompt_versions={**LAYER_VERSIONS, "JUDGEMENT": JUDGEMENT_VERSION},
                    # 본문 호출이 실제로 나간 공급자(KNK-674). 판정도 같은 CHAT_MODEL이라
                    # 값이 같다 — 두 값이 갈릴 수 있게 되면 그때 meta 계약부터 정한다.
                    #
                    # 키가 빠졌을 때만 되짚어 채운다. 여기서 KeyError가 나면 이미 200으로
                    # 열린 SSE라 상태를 못 바꾸고, error 이벤트도 completed도 없이 끊긴다 —
                    # 사용자 화면엔 글이 떴는데 백엔드는 그 턴을 저장하지 못한다(KNK-674
                    # 리뷰 M2에서 실제 재현). 지금 키가 빠지는 경로는 없다.
                    #
                    # **`or`를 쓰지 않는다.** 빈 문자열까지 폴백을 타서, "공급자를 못 구했다"는
                    # 고장 신호가 그럴듯한 값으로 덮인다 — 이 티켓이 없애려던 "실제 호출과
                    # 무관한 값"이 그대로 재현된다(KNK-674 2차 리뷰). 빈 값은 빈 값으로 둔다.
                    provider=(
                        ev["provider"]
                        if "provider" in ev
                        else llm.provider_of(settings.chat_model)
                    ),
                    input_token_count=_add_tokens(ev.get("input_tokens"), judgement.input_tokens),
                    output_token_count=_add_tokens(ev.get("output_tokens"), judgement.output_tokens),
                    retry_count=0,
                )
                payload = CompletedData(
                    ai_output=ai_output,
                    # 하위호환 빈 배열 — 백엔드는 '빈 배열이면 저장하지 않음'(4-backend §4-3-3).
                    # 프론트·백엔드 전환 완료 후 필드 제거를 검토한다.
                    choices=[],
                    meta=meta,
                    target_main_event=judgement.target_main_event,
                    occurred_main_event_name=judgement.occurred_main_event_name,
                    ending_name=judgement.ending_name,
                ).model_dump(by_alias=True)  # aiOutput·camelCase 메타로 직렬화
                yield _sse(EVENT_COMPLETED, payload)
            else:  # EVENT_ERROR
                yield _sse(name, ErrorData(code=ev["code"], message=ev["message"]).model_dump())


@router.post("/chat/turns")
async def chat_turn(request: ChatTurnRequest) -> StreamingResponse:
    """채팅 한 턴을 SSE로 스트리밍한다(완전 stateless — 받은 재료로 조립·응답만)."""
    return StreamingResponse(_event_stream(request), media_type="text/event-stream")


@router.post("/chat/choices", response_model=ChatChoicesResponse)
async def chat_choices(request: ChatChoicesRequest) -> ChatChoicesResponse:
    """다음 행동 선택지 3개를 생성한다 — /chat/turns에서 분리된 전용 호출(KNK-625).

    generate_choices가 부족·실패를 재호출·폴백으로 흡수해 정확히 3개를 보장하므로,
    **유효한 요청이면 LLM 생성 실패도 항상 200**이다(스키마 위반 요청은 FastAPI 422).
    동기 REST라 표기는 snake_case(story 계열과 동일 — camelCase는 chat SSE
    completed만의 공식 예외라 넓히지 않는다).
    """
    # 누적 재호출(최대 3회)까지 한 트레이스로 묶인다(KNK-624). 분석 차원 부착(KNK-640):
    # 프롬프트 버전은 미리, 재호출 횟수는 생성 결과에서 사후에 싣는다.
    # 장르 태그는 스토리 제작 트레이스에만 — 채팅 쪽은 KNK-652에서 제거(5-ai-server §5-6).
    with observe_request(
        "채팅 선택지",
        metadata={"prompt_versions": {"NEXT_ACTIONS": NEXT_ACTIONS_VERSION}},
    ) as trace:
        result = await generate_choices(request, request.ai_output)
        trace.set_metadata(retry_count=result.retry_count)
        meta = StoryResponseMeta(
            model=result.model,
            prompt_versions={"NEXT_ACTIONS": NEXT_ACTIONS_VERSION},
            provider=result.provider,
            input_token_count=result.input_tokens,
            output_token_count=result.output_tokens,
            retry_count=result.retry_count,
        )
        return ChatChoicesResponse(choices=result.choices, meta=meta)
