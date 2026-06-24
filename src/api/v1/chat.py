"""채팅 턴 API (시점 B) — SSE 스트리밍 엔드포인트.

`POST /api/v1/chat/turns`. ChatTurnRequest를 받아 6레이어 프롬프트를 조립하고
(`assemble`), LLM을 스트리밍 호출해(`stream_chat_turn`) 토큰을 실시간으로 흘린다.

AI가 발행하는 SSE 이벤트는 `token`·`completed`·`error` 3개뿐이다(명세 B). `started`·
`chatId`·`turnId`는 백엔드(manyak-server)가 부착하므로 여기서 넣지 않는다. `completed`의
`ai_output`은 와이어 계약 키 `aiOutput`으로 직렬화한다(by_alias=True).
"""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.core.config import settings
from src.schemas.chat_turn import (
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_TOKEN,
    ChatTurnRequest,
    CompletedData,
    ErrorData,
    TokenData,
)
from src.schemas.response_meta import ChatResponseMeta
from src.services.chat_assembler import LAYER_VERSIONS, assemble
from src.services.chat_llm import stream_chat_turn

router = APIRouter()


def _sse(event: str, data: dict) -> str:
    """SSE 한 프레임으로 직렬화한다(`event:`/`data:` 한 줄씩 + 빈 줄로 종료)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(req: ChatTurnRequest) -> AsyncIterator[str]:
    """조립 → LLM 스트리밍 → SSE 프레임. 스키마로 페이로드를 검증·직렬화한다."""
    messages = assemble(req)
    async for ev in stream_chat_turn(messages):
        name = ev["event"]
        if name == EVENT_TOKEN:
            yield _sse(name, TokenData(text=ev["text"]).model_dump())
        elif name == EVENT_COMPLETED:
            meta = ChatResponseMeta(
                model=ev.get("model") or settings.deepseek_chat_model,
                prompt_versions=LAYER_VERSIONS,
                provider=settings.llm_provider,
                input_token_count=ev.get("input_tokens"),
                output_token_count=ev.get("output_tokens"),
                retry_count=0,  # chat은 단일 스트림 호출 — 재호출 없음
            )
            payload = CompletedData(
                ai_output=ev["ai_output"], choices=ev["choices"], meta=meta
            ).model_dump(by_alias=True)  # aiOutput·camelCase 메타로 직렬화
            yield _sse(name, payload)
        else:  # EVENT_ERROR
            yield _sse(name, ErrorData(code=ev["code"], message=ev["message"]).model_dump())


@router.post("/chat/turns")
async def chat_turn(request: ChatTurnRequest) -> StreamingResponse:
    """채팅 한 턴을 SSE로 스트리밍한다(완전 stateless — 받은 재료로 조립·응답만)."""
    return StreamingResponse(_event_stream(request), media_type="text/event-stream")
