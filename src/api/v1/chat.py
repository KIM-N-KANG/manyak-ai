"""채팅 API (시점 B) — 턴 SSE 스트리밍 + 선택지 동기 엔드포인트.

`POST /api/v1/chat/turns`: ChatTurnRequest를 받아 6레이어 프롬프트를 조립하고
(`assemble`), 본문을 스트리밍 호출해(`stream_chat_turn`) 토큰을 흘린다. 자식 이미지 사용 시
본문이 끝나면 이미지 생성을 시작한다. 이미지·대사를 보낸 뒤 본문과 meta를 completed 이벤트로 발행한다.
**completed가 선택지를 기다리지 않는다** — 선택지는 전용 엔드포인트로 분리됐다(KNK-625, 지연 해소).

`POST /api/v1/chat/choices`: 분리된 선택지 생성(동기 REST). 백엔드가 completed 이후
같은 재료 + 방금 본문(ai_output)으로 호출한다. 항상 200 + 정확히 3개(폴백 흡수).

AI가 발행하는 SSE 이벤트는 token·character_image·completed·error·ping 5개다. ping은 이미지용
본문·이미지를 기다리는 동안 나가는 신호로, 백엔드의 이벤트 간 상한 시계를 되돌린다(KNK-750 — `EVENT_PING` 주석).
started·chatId·turnId는 백엔드(manyak-server)가 부착한다. completed의 ai_output·character_images·meta는
와이어 계약 키(aiOutput·characterImages·camelCase)로 직렬화한다(by_alias=True). completed의 choices는 하위호환 빈 배열 고정 —
백엔드는 '빈 배열이면 저장하지 않음'(spec/4-backend-server-spec.md §4-3-3)이라 선행 배포에 안전하다.
판정 메타 3필드(targetMainEvent·occurredMainEventName·endingName)는 판정 제거(KNK-1626)와 함께
completed에서 뺐다. 백엔드는 없는 필드를 null로 읽어 동작이 같다.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import asdict

from anyio import CancelScope
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from src.core.langfuse import observe_request
from src.core.request_context import ConnectionMetadata, select_connection_metadata
from src.schemas.chat_choices import ChatChoicesRequest, ChatChoicesResponse
from src.schemas.chat_compact import ChatCompactRequest, ChatCompactResponse
from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE,
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_PING,
    EVENT_TOKEN,
    CharacterImageData,
    ChatTurnRequest,
    CompletedData,
    ErrorData,
    PingData,
    TokenData,
)
from src.schemas.response_meta import ChatResponseMeta, StoryResponseMeta
from src.services import llm
from src.services.chat_assembler import assemble, body_prompt_versions
from src.services.chat_llm import select_chat_model, stream_chat_turn
from src.services.chat_selected_images import stream_with_selected_images
from src.services.chat_child_image import ChildImageObservation, stream_with_child_image
from src.services.chat_choices import NEXT_ACTIONS_VERSION, generate_choices
from src.services.chat_compact import CompactProgress, generate_compact

router = APIRouter()


async def _compact_result(payload: ChatCompactRequest) -> ChatCompactResponse:
    summary = await generate_compact(payload, CompactProgress())
    return ChatCompactResponse(summary=summary)


async def _wait_compact_disconnect(request: Request) -> None:
    # FastAPI가 요청 본문을 읽은 뒤 호출하므로 다음 ASGI 메시지를 기다릴 수 있다.
    while (await request.receive())["type"] != "http.disconnect":
        pass


@router.post("/chat/compact", response_model=ChatCompactResponse)
async def chat_compact(payload: ChatCompactRequest, request: Request) -> ChatCompactResponse:
    generation = asyncio.create_task(_compact_result(payload))
    disconnect = asyncio.create_task(_wait_compact_disconnect(request))
    try:
        done, _ = await asyncio.wait({generation, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            disconnect.result()
            raise asyncio.CancelledError()
        return await generation
    finally:
        generation.cancel()
        disconnect.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(generation, disconnect, return_exceptions=True)

# 백엔드가 SSE 연결 하나에 허용하는 전체 시간(초).
#
# **이건 백엔드가 정한 값이다**(manyak-server `ChatService.SSE_TIMEOUT_MILLIS = 120_000`).
# 백엔드가 이 값을 바꾸면 여기도 같이 바꿔야 한다 — 어긋나면 이미지 생성에 실제보다 넉넉한
# 시간을 줘서 턴이 죽는다. ping은 이벤트 간 상한만 되돌릴 뿐 이 시계는 못 멈춘다.
#
# 백엔드는 우리에게 요청을 보내기 **전에** 이 시계를 켠다. 그래서 아래에서 재는 경과 시간은
# 실제보다 조금 짧게 잡히는데, 그 차이도 완료 여유(_COMPLETED_MARGIN_SECONDS)가 함께 덮는다.
_TURN_BUDGET_SECONDS = 120.0

# 남은 시간에서 미리 떼어 두는 안전 여유(초). 일부러 넉넉하게 잡는다.
#
# 덮어야 하는 것이 셋이다. ①백엔드는 우리에게 요청을 보내기 **전에** 120초 시계를 켜므로
# 아래 경과 시간은 실제보다 짧게 잡힌다. ②그 사이에 백엔드 워커 대기 줄(`ChatSseConfig` —
# core 4 · queue 100)에서 몇 초 밀릴 수 있다. ③이미지가 끝난 뒤 글과 completed를 보내는
# 시간도 필요하다. 셋 다 우리가 측정할 수 없어서, 정확히 맞히는 대신 **넉넉히 떼어 두고
# 이미지 쪽이 손해 보게** 한다. 이미지를 조금 덜 기다리는 손해가 턴이 죽는 손해보다 훨씬 싸다.
#
# 정석은 백엔드가 남은 시간을 요청에 실어 보내는 것이다. 그건 계약 변경이라 별도 사안이고,
# 그 전까지 이 값은 **추정**이지 보장이 아니다.
_SAFETY_MARGIN_SECONDS = 15.0


def _sse(event: str, data: dict) -> str:
    """SSE 한 프레임으로 직렬화한다(`event:`/`data:` 한 줄씩 + 빈 줄로 종료)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(
    req: ChatTurnRequest, connection_metadata: ConnectionMetadata
) -> AsyncGenerator[str, None]:
    """본문과 선택적 이미지 생성을 실행하고 SSE 프레임으로 낸다.

    본문 스트림이 error로 끝나면 그 error만 relay한다.

    트레이스(KNK-624)는 핸들러가 아니라 **이 제너레이터 안에서** 연다 — SSE는 응답이 200으로
    열린 뒤에 본 작업이 돌기 때문에, 핸들러가 반환하는 시점에는 아직 LLM 호출 전이다. 블록이
    스트림 전체를 감싸므로 본문·이미지 호출이 한 트레이스에 묶인다.
    """
    # 분석 차원 부착(KNK-640): 본문 프롬프트 버전을 싣는다. 채팅 턴은 재호출이 없어 retry_count=0.
    # 관측과 completed meta가 같은 단일 채팅 템플릿 버전을 쓰도록 한 번만 구한다.
    prompt_versions = body_prompt_versions()
    selected_model, _ = select_chat_model(req.user_id, req.model_tier)
    selected_provider = llm.provider_of(selected_model)
    # 장르 태그는 스토리 제작 트레이스에만 — 채팅 쪽은 KNK-652에서 제거(spec/5-ai-server-spec.md §5-6).
    with observe_request(
        "채팅 턴",
        # 업로드 슬롯에는 임시 쓰기 권한이 담긴 서명 URL이 있어 관측에서 제외한다.
        input_data=req.model_dump(mode="json", exclude={"image_slots"}),
        metadata={
            **connection_metadata,
            "user_id": str(req.user_id) if req.user_id is not None else None,
            "model_tier": req.model_tier,
            "model": selected_model,
            "provider": selected_provider,
            "prompt_versions": prompt_versions,
            "retry_count": 0,
        },
    ) as trace:
        # 이 턴에 쓴 시간을 잰다 — 이미지 생성에 얼마를 줄 수 있는지가 여기서 나온다(위 상수 참조).
        turn_started = time.monotonic()
        messages = assemble(req)
        image_observation = ChildImageObservation() if req.image_slots else None

        events = stream_chat_turn(
            messages, character_images=req.character_images,
            user_id=req.user_id, model_tier=req.model_tier,
        )
        if req.image_slots:
            events = stream_with_child_image(
                events, req,
                deadline=turn_started + _TURN_BUDGET_SECONDS - _SAFETY_MARGIN_SECONDS,
                observation=image_observation,
            )
        else:
            events = stream_with_selected_images(events, req)
        try:
            async with aclosing(events):
                async for ev in events:
                    name = ev["event"]
                    if name == EVENT_TOKEN:
                        yield _sse(name, TokenData(text=ev["text"]).model_dump())
                    elif name == EVENT_CHARACTER_IMAGE:
                        payload = CharacterImageData(
                            name=ev["name"],
                            image_name=ev["image_name"],
                            image_url=ev["image_url"],
                        ).model_dump(by_alias=True)
                        yield _sse(name, payload)
                    elif name == EVENT_PING:
                        yield _sse(name, PingData().model_dump())
                    elif name == EVENT_COMPLETED:
                        # retry_count는 0 고정 — 본문은 재호출이 없고, 선택지 재호출 횟수는
                        # /chat/choices 응답 meta로 이동했다(NEXT_ACTIONS 버전 키도 함께 이동).
                        meta = ChatResponseMeta(
                            model=ev.get("model") or selected_model,
                            prompt_versions=prompt_versions,
                            # 본문 호출이 실제로 나간 공급자(KNK-674).
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
                                else selected_provider
                            ),
                            input_token_count=ev.get("input_tokens"),
                            output_token_count=ev.get("output_tokens"),
                            retry_count=0,
                        )
                        trace.set_metadata(model=meta.model, provider=meta.provider)
                        payload = CompletedData(
                            ai_output=ev["ai_output"],
                            # 하위호환 빈 배열 — 백엔드는 '빈 배열이면 저장하지 않음'(spec/4-backend-server-spec.md §4-3-3).
                            # 프론트·백엔드 전환 완료 후 필드 제거를 검토한다.
                            choices=[],
                            character_images=ev.get("character_images", []),
                            meta=meta,
                        ).model_dump(by_alias=True)  # aiOutput·camelCase 메타로 직렬화
                        yield _sse(EVENT_COMPLETED, payload)
                    else:  # EVENT_ERROR
                        yield _sse(name, ErrorData(code=ev["code"], message=ev["message"]).model_dump())
        finally:
            if image_observation is not None:
                trace.set_metadata(child_image=asdict(image_observation))


class _ChatStreamingResponse(StreamingResponse):
    """전송 중 연결이 끊겨도 채팅 호출의 취소·정리를 끝낸다."""

    def __init__(self, events: AsyncGenerator[str, None]) -> None:
        super().__init__(events, media_type="text/event-stream")
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # send()에서 끊기면 제너레이터 내부 finally에 도달하지 않으므로 직접 닫는다.
            with CancelScope(shield=True):
                await self._events.aclose()


@router.post("/chat/turns")
async def chat_turn(request: ChatTurnRequest) -> StreamingResponse:
    """채팅 한 턴을 SSE로 스트리밍한다(완전 stateless — 받은 재료로 조립·응답만)."""
    # ContextVar도 자식 태스크에 복사돼 안전하지만, 요청별 연결값 전달을 코드에 명확히
    # 드러내기 위해 핸들러에서 스냅샷을 만들어 제너레이터에 넘긴다(KNK-770).
    connection_metadata = select_connection_metadata(
        "creation_id",
        "story_id",
        "chat_id",
        "start_setting_id",
        "turn_number",
        "is_regenerated",
    )
    if request.user_source is not None:
        connection_metadata["user_source"] = request.user_source
    return _ChatStreamingResponse(_event_stream(request, connection_metadata))


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
    # 장르 태그는 스토리 제작 트레이스에만 — 채팅 쪽은 KNK-652에서 제거(spec/5-ai-server-spec.md §5-6).
    with observe_request(
        "채팅 선택지",
        input_data=request.model_dump(mode="json", exclude={"user_source", "image_slots"}),
        metadata={
            **select_connection_metadata(
                "creation_id",
                "story_id",
                "chat_id",
                "start_setting_id",
                "turn_number",
                "is_regenerated",
            ),
            "prompt_versions": {"NEXT_ACTIONS": NEXT_ACTIONS_VERSION},
        },
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
