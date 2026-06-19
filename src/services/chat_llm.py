"""채팅 LLM 통로(ChatProvider) — 스트리밍 호출 + 본문/선택지 분리 (시점 B).

`assemble()`가 만든 messages를 Upstage(OpenAI 호환) `stream=True`로 호출하고,
토큰을 실시간으로 흘리되 `[다음 행동]` 마커부터는 선택지로 보고 흘리지 않는다(B안).
스트림이 끝나면 통짜 출력을 본문(`ai_output`)과 선택지(`choices`)로 갈라 completed로 낸다.

이벤트(dict)를 async generator로 낸다. SSE 와이어 변환은 엔드포인트(chat.py)가 맡는다.
- {"event": "token",     "text": ...}
- {"event": "completed", "ai_output": ..., "choices": [...]}
- {"event": "error",     "code": ..., "message": ...}
"""

import re
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, OpenAIError

from src.core.config import settings
from src.schemas.chat_turn import EVENT_COMPLETED, EVENT_ERROR, EVENT_TOKEN

_client = AsyncOpenAI(
    api_key=settings.upstage_api_key,
    base_url="https://api.upstage.ai/v1",
    timeout=90.0,  # 첫 토큰까지의 상한. 스트리밍이라 통상 빠르게 시작된다.
)

# 선택지 영역 시작 마커(CORE 출력 봉투, 2.5). 이 앞까지가 본문(ai_output)이다.
_CHOICES_MARKER = "[다음 행동]"


def _parse_choices(text: str) -> list[str]:
    """`[다음 행동]` 이후 텍스트에서 `1./2./3.` 선택지 문구만 뽑는다."""
    return [m.strip() for m in re.findall(r"^\s*[1-3][.)]\s*(.+?)\s*$", text, re.M)]


def _split_output(full: str) -> tuple[str, list[str]]:
    """LLM 통짜 출력을 본문(ai_output)과 선택지(choices)로 가른다(명세 2.5).

    `[다음 행동]` 마커가 없으면 전체를 본문으로 보고 choices는 빈 리스트로 둔다.
    """
    idx = full.find(_CHOICES_MARKER)
    if idx == -1:
        return full.strip(), []
    return full[:idx].strip(), _parse_choices(full[idx + len(_CHOICES_MARKER):])


async def stream_chat_turn(messages: list[dict]) -> AsyncIterator[dict]:
    """messages를 LLM에 스트리밍 호출하고 token→completed(또는 error) 이벤트를 낸다.

    B안: `[다음 행동]` 마커 전까지의 본문만 token으로 흘리고, 마커부터는 흘리지 않는다
    (선택지는 completed의 choices로만 전달). 마커가 토큰 경계에 걸칠 수 있어, 끝
    (마커 길이-1)글자는 버퍼에 남겨 두고 나머지만 흘려 마커가 쪼개져도 놓치지 않는다.
    """
    full = ""          # 전체 누적 — 완료 시 본문/선택지 분리용
    pending = ""       # 아직 흘리지 않은 본문 버퍼 — 마커 prefix 겹침 보호
    body_done = False  # 마커를 만나 본문이 끝났는지

    try:
        stream = await _client.chat.completions.create(
            model=settings.upstage_model,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            full += delta
            if body_done:
                continue  # 마커 이후(선택지)는 흘리지 않고 누적만 한다
            pending += delta
            idx = pending.find(_CHOICES_MARKER)
            if idx != -1:
                head = pending[:idx]
                if head:
                    yield {"event": EVENT_TOKEN, "text": head}
                body_done = True
                pending = ""
                continue
            # 마커가 경계에 걸칠 수 있으니 끝 (마커 길이-1)글자는 보류하고 나머지만 흘린다.
            safe = len(pending) - (len(_CHOICES_MARKER) - 1)
            if safe > 0:
                yield {"event": EVENT_TOKEN, "text": pending[:safe]}
                pending = pending[safe:]

        # 스트림 끝 — 마커를 못 만났다면 남은 본문 버퍼를 흘린다.
        if not body_done and pending:
            yield {"event": EVENT_TOKEN, "text": pending}

        ai_output, choices = _split_output(full)
        yield {"event": EVENT_COMPLETED, "ai_output": ai_output, "choices": choices}
    except OpenAIError as e:
        yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": str(e)}
