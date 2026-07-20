"""채팅 턴 API (시점 B) — SSE 스트리밍 엔드포인트.

`POST /api/v1/chat/turns`. ChatTurnRequest를 받아 6레이어 프롬프트를 조립하고
(`assemble`), 본문을 스트리밍 호출해(`stream_chat_turn`) 토큰을 흘린다. 본문 스트림이
끝나면 선택지 전용 호출(`generate_choices` — 항상 정확히 3개 보장)과 사건·엔딩 판정
호출(`generate_judgement` — 재료 없으면 스킵, 실패하면 null)을 병렬로 실행해,
본문·선택지·판정 메타·합산 meta를 completed 이벤트 하나로 합쳐 발행한다.

AI가 발행하는 SSE 이벤트는 token·completed·error 3개뿐이다(명세 B). started·chatId·turnId는
백엔드(manyak-server)가 부착한다. completed의 ai_output·meta는 와이어 계약 키(aiOutput·
camelCase)로 직렬화한다(by_alias=True). 선택지는 completed의 choices로만 나가며 토큰으로
흘리지 않는다. completed에 추가된 판정 메타 3필드(targetMainEvent·occurredMainEventName·
endingName)는 재료 없는 요청에서 null이고, 서버 DTO가 ignoreUnknown이라 선행 배포에 안전하다.
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.core.config import settings
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
from src.services.chat_assembler import LAYER_VERSIONS, assemble
from src.services.chat_llm import stream_chat_turn
from src.services.chat_choices import NEXT_ACTIONS_VERSION, generate_choices
from src.services.chat_judgement import JUDGEMENT_VERSION, generate_judgement

router = APIRouter()


def _sse(event: str, data: dict) -> str:
    """SSE 한 프레임으로 직렬화한다(`event:`/`data:` 한 줄씩 + 빈 줄로 종료)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _add_tokens(a: int | None, b: int | None) -> int | None:
    """본문·선택지 두 호출의 토큰을 합산한다. 둘 다 None이면 None, 아니면 누락을 0으로 본다."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


async def _event_stream(req: ChatTurnRequest) -> AsyncIterator[str]:
    """조립 → 본문 스트리밍 → (종료 후) 선택지 호출 → 합쳐 SSE 프레임으로 낸다.

    본문 스트림이 error로 끝나면 그 error만 relay하고 선택지 호출은 하지 않는다.
    """
    messages = assemble(req)
    async for ev in stream_chat_turn(messages):
        name = ev["event"]
        if name == EVENT_TOKEN:
            yield _sse(name, TokenData(text=ev["text"]).model_dump())
        elif name == EVENT_COMPLETED:
            ai_output = ev["ai_output"]
            # 본문이 끝난 뒤 선택지·판정 호출을 병렬 실행한다(plan-1 설계 결정) —
            # 선택지는 항상 3개 보장, 판정은 재료 없으면 스킵·실패하면 null(둘 다 턴을 깨지 않음).
            choices_result, judgement = await asyncio.gather(
                generate_choices(req, ai_output),
                generate_judgement(req, ai_output),
            )
            # 메타 합산: 토큰은 본문+선택지+판정, prompt_versions는 6레이어+NEXT_ACTIONS+JUDGEMENT,
            # retry_count는 선택지 재호출 횟수. model·provider는 단일(같은 v4-flash·deepseek).
            meta = ChatResponseMeta(
                model=ev.get("model") or settings.chat_model,
                prompt_versions={
                    **LAYER_VERSIONS,
                    "NEXT_ACTIONS": NEXT_ACTIONS_VERSION,
                    "JUDGEMENT": JUDGEMENT_VERSION,
                },
                provider=settings.llm_provider,
                input_token_count=_add_tokens(
                    _add_tokens(ev.get("input_tokens"), choices_result.input_tokens),
                    judgement.input_tokens,
                ),
                output_token_count=_add_tokens(
                    _add_tokens(ev.get("output_tokens"), choices_result.output_tokens),
                    judgement.output_tokens,
                ),
                retry_count=choices_result.retry_count,
            )
            payload = CompletedData(
                ai_output=ai_output,
                choices=choices_result.choices,
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

    generate_choices가 부족·실패를 재호출·폴백으로 흡수해 정확히 3개를 보장하므로
    이 엔드포인트는 항상 200이다. 동기 REST라 표기는 snake_case(story 계열과 동일 —
    camelCase는 chat SSE completed만의 공식 예외라 넓히지 않는다).
    """
    result = await generate_choices(request, request.ai_output)
    meta = StoryResponseMeta(
        model=result.model,
        prompt_versions={"NEXT_ACTIONS": NEXT_ACTIONS_VERSION},
        provider=settings.llm_provider,
        input_token_count=result.input_tokens,
        output_token_count=result.output_tokens,
        retry_count=result.retry_count,
    )
    return ChatChoicesResponse(choices=result.choices, meta=meta)
