"""채팅 API (시점 B) — 턴 SSE 스트리밍 + 선택지 동기 엔드포인트.

`POST /api/v1/chat/turns`: ChatTurnRequest를 받아 6레이어 프롬프트를 조립하고
(`assemble`), 본문을 스트리밍 호출해(`stream_chat_turn`) 토큰을 흘린다. 본문 스트림이
끝나면 사건·엔딩 판정을 호출한다. 자식 이미지 사용 시 이미지 생성도 동시에 시작한다.
이미지·대사를 보낸 뒤 판정까지 끝나면 본문·판정 메타·합산 meta를 completed 이벤트로 발행한다. **completed가 선택지를
기다리지 않는다** — 선택지는 전용 엔드포인트로 분리됐다(KNK-625, 지연 해소).

`POST /api/v1/chat/choices`: 분리된 선택지 생성(동기 REST). 백엔드가 completed 이후
같은 재료 + 방금 본문(ai_output)으로 호출한다. 항상 200 + 정확히 3개(폴백 흡수).

AI가 발행하는 SSE 이벤트는 token·character_image·completed·error·ping 5개다. ping은 판정이나
자식 이미지용 본문·이미지를 기다리는 동안 나가는 신호로, 백엔드의 이벤트 간 상한 시계를 되돌린다(KNK-750 — `EVENT_PING` 주석).
started·chatId·turnId는 백엔드(manyak-server)가 부착한다. completed의 ai_output·character_images·meta는
와이어 계약 키(aiOutput·characterImages·camelCase)로 직렬화한다(by_alias=True). completed의 choices는 하위호환 빈 배열 고정 —
백엔드는 '빈 배열이면 저장하지 않음'(spec/4-backend-server-spec.md §4-3-3)이라 선행 배포에 안전하다.
판정 메타 3필드(targetMainEvent·occurredMainEventName·endingName)는 재료 없는 요청에서
null이고, 서버 DTO가 ignoreUnknown이라 역시 선행 배포에 안전하다.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing

from anyio import CancelScope
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from src.core.config import settings
from src.core.langfuse import observe_request
from src.core.request_context import ConnectionMetadata, select_connection_metadata
from src.schemas.chat_choices import ChatChoicesRequest, ChatChoicesResponse
from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE,
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_PING,
    EVENT_TOKEN,
    CharacterImageData,
    GeneratedChildImageData,
    ChatTurnRequest,
    CompletedData,
    ErrorData,
    PingData,
    TokenData,
)
from src.schemas.response_meta import ChatResponseMeta, StoryResponseMeta
from src.services import llm
from src.services.chat_assembler import LAYER_VERSIONS, assemble
from src.services.chat_llm import stream_chat_turn
from src.services.chat_child_image import stream_with_child_image
from src.services.chat_choices import NEXT_ACTIONS_VERSION, generate_choices
from src.services.chat_judgement import JUDGEMENT_VERSION, generate_judgement

router = APIRouter()

# 판정을 기다리는 동안 ping을 내보내는 간격(초). 백엔드의 이벤트 간 상한(60초)보다 확실히
# 짧아야 시계가 다 차기 전에 되돌아간다. 환경변수로 빼지 않는다 — 바꿀 일이 거의 없는 값이고,
# 늘리면 manyak-infra의 Compose 설정까지 함께 맞춰야 한다.
_JUDGEMENT_PING_INTERVAL_SECONDS = 10.0

# 백엔드가 SSE 연결 하나에 허용하는 전체 시간(초).
#
# **이건 백엔드가 정한 값이다**(manyak-server `ChatService.SSE_TIMEOUT_MILLIS = 120_000`).
# 백엔드가 이 값을 바꾸면 여기도 같이 바꿔야 한다 — 어긋나면 판정에 실제보다 넉넉한 시간을
# 줘서 턴이 죽는다. ping은 이벤트 간 상한만 되돌릴 뿐 이 시계는 못 멈춘다.
#
# 백엔드는 우리에게 요청을 보내기 **전에** 이 시계를 켠다. 그래서 아래에서 재는 경과 시간은
# 실제보다 조금 짧게 잡히는데, 그 차이도 완료 여유(_COMPLETED_MARGIN_SECONDS)가 함께 덮는다.
_TURN_BUDGET_SECONDS = 120.0

# 남은 시간에서 미리 떼어 두는 안전 여유(초). 일부러 넉넉하게 잡는다.
#
# 덮어야 하는 것이 셋이다. ①백엔드는 우리에게 요청을 보내기 **전에** 120초 시계를 켜므로
# 아래 경과 시간은 실제보다 짧게 잡힌다. ②그 사이에 백엔드 워커 대기 줄(`ChatSseConfig` —
# core 4 · queue 100)에서 몇 초 밀릴 수 있다. ③판정이 끝난 뒤 completed를 만들어 보내는
# 시간도 필요하다. 셋 다 우리가 측정할 수 없어서, 정확히 맞히는 대신 **넉넉히 떼어 두고
# 판정 쪽이 손해 보게** 한다. 판정을 조금 덜 주는 손해가 턴이 죽는 손해보다 훨씬 싸다.
#
# 여유를 크게 잡아도 평소에는 아무것도 안 바뀐다 — 본문이 45초 안에 끝나면 판정은 여전히
# 상한 60초를 그대로 받는다(관측된 본문 최대 25.5초).
#
# 정석은 백엔드가 남은 시간을 요청에 실어 보내는 것이다. 그건 계약 변경이라 별도 사안이고,
# 그 전까지 이 값은 **추정**이지 보장이 아니다.
_SAFETY_MARGIN_SECONDS = 15.0


def _sse(event: str, data: dict) -> str:
    """SSE 한 프레임으로 직렬화한다(`event:`/`data:` 한 줄씩 + 빈 줄로 종료)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _add_tokens(a: int | None, b: int | None) -> int | None:
    """본문·판정 두 호출의 토큰을 합산한다. 둘 다 None이면 None, 아니면 누락을 0으로 본다."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


async def _event_stream(
    req: ChatTurnRequest, connection_metadata: ConnectionMetadata
) -> AsyncGenerator[str, None]:
    """본문 완성 후 판정과 선택적 이미지 생성을 병렬로 실행하고 SSE 프레임으로 낸다.

    본문 스트림이 error로 끝나면 그 error만 relay하고 판정 호출은 하지 않는다.

    트레이스(KNK-624)는 핸들러가 아니라 **이 제너레이터 안에서** 연다 — SSE는 응답이 200으로
    열린 뒤에 본 작업이 돌기 때문에, 핸들러가 반환하는 시점에는 아직 LLM 호출 전이다. 블록이
    스트림 전체를 감싸므로 본문·판정 두 호출이 한 트레이스에 묶인다.
    """
    # 분석 차원 부착(KNK-640): 6레이어+판정 버전을 싣는다. 채팅 턴은 재호출이 없어 retry_count=0.
    # 장르 태그는 스토리 제작 트레이스에만 — 채팅 쪽은 KNK-652에서 제거(spec/5-ai-server-spec.md §5-6).
    with observe_request(
        "채팅 턴",
        input_data=req.model_dump(mode="json"),
        metadata={
            **connection_metadata,
            "prompt_versions": {**LAYER_VERSIONS, "JUDGEMENT": JUDGEMENT_VERSION},
            "retry_count": 0,
        },
    ):
        # 이 턴에 쓴 시간을 잰다 — 판정에 얼마를 줄 수 있는지가 여기서 나온다(아래 참조).
        turn_started = time.monotonic()
        messages = assemble(req)
        judging = None

        def start_judgement(ai_output: str) -> None:
            nonlocal judging
            remaining = _TURN_BUDGET_SECONDS - (time.monotonic() - turn_started) - _SAFETY_MARGIN_SECONDS
            judging = asyncio.create_task(
                generate_judgement(req, ai_output, budget_seconds=remaining)
            )

        events = stream_chat_turn(messages, character_images=req.character_images)
        if req.generate_child_image:
            events = stream_with_child_image(
                events, req,
                deadline=turn_started + _TURN_BUDGET_SECONDS - _SAFETY_MARGIN_SECONDS,
                on_body_completed=start_judgement,
            )
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
                        if "generated_image" in ev:
                            payload["generatedImage"] = GeneratedChildImageData(
                                **ev["generated_image"]
                            ).model_dump(by_alias=True)
                        yield _sse(name, payload)
                    elif name == EVENT_PING:
                        yield _sse(name, PingData().model_dump())
                    elif name == EVENT_COMPLETED:
                        ai_output = ev["ai_output"]
                        # 이미지 사용 시 본문 완성 직후 이미 시작한 판정을 기다린다.
                        # 기존 요청은 본문 스트리밍 완료 시점에 시작한다.
                        if judging is None:
                            start_judgement(ai_output)
                        while True:
                            done, _ = await asyncio.wait(
                                {judging}, timeout=_JUDGEMENT_PING_INTERVAL_SECONDS
                            )
                            if done:
                                break
                            yield _sse(EVENT_PING, PingData().model_dump())
                        judgement = judging.result()
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
                            # 하위호환 빈 배열 — 백엔드는 '빈 배열이면 저장하지 않음'(spec/4-backend-server-spec.md §4-3-3).
                            # 프론트·백엔드 전환 완료 후 필드 제거를 검토한다.
                            choices=[],
                            character_images=ev.get("character_images", []),
                            meta=meta,
                            target_main_event=judgement.target_main_event,
                            occurred_main_event_name=judgement.occurred_main_event_name,
                            ending_name=judgement.ending_name,
                        ).model_dump(by_alias=True)  # aiOutput·camelCase 메타로 직렬화
                        yield _sse(EVENT_COMPLETED, payload)
                    else:  # EVENT_ERROR
                        yield _sse(name, ErrorData(code=ev["code"], message=ev["message"]).model_dump())
        finally:
            if judging is not None:
                with CancelScope(shield=True):
                    if not judging.done() and not judging.cancelling():
                        judging.cancel()
                    await asyncio.gather(judging, return_exceptions=True)


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
        input_data=request.model_dump(mode="json", exclude={"user_source"}),
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
