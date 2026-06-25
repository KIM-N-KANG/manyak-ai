"""채팅 LLM 통로(ChatProvider) — 스트리밍 호출 + 본문/선택지 분리 (시점 B).

`assemble()`가 만든 messages를 Upstage(OpenAI 호환) `stream=True`로 호출하고,
토큰을 실시간으로 흘리되 `[다음 행동]` 마커부터는 선택지로 보고 흘리지 않는다(B안).
스트림이 끝나면 통짜 출력을 본문(`ai_output`)과 선택지(`choices`)로 갈라 completed로 낸다.

이벤트(dict)를 async generator로 낸다. SSE 와이어 변환은 엔드포인트(chat.py)가 맡는다.
- {"event": "token",     "text": ...}
- {"event": "completed", "ai_output": ..., "choices": [...]}
- {"event": "error",     "code": ..., "message": ...}
"""

import logging
import re
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, OpenAIError

from src.core.config import settings
from src.core.sentry import FEATURE_CHAT_RESPONSE, capture_ai_exception
from src.schemas.chat_turn import EVENT_COMPLETED, EVENT_ERROR, EVENT_TOKEN
from src.services.chat_assembler import LAYER_VERSIONS

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_api_url,
    timeout=90.0,  # 첫 토큰까지의 상한. 스트리밍이라 통상 빠르게 시작된다.
)

# DeepSeek V4를 비추론으로 호출한다(thinking 비활성). 추론 모드는 첫 토큰 지연이
# 크고 창작 출력 품질도 비추론이 더 안정적이었다(KNK-208 벤치).
_THINKING_DISABLED = {"thinking": {"type": "disabled"}}

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


# 줄머리 볼드 화자 라벨을 평문으로 정규화한다: `**설하:**`·`**설하**:` → `설하:`.
# 모델이 사전학습 편향으로 화자 이름을 볼드로 감싸는 경향이 강해(프롬프트로 못 막음 —
# KNK-194 검증에서 100건 중 약 절반 발생) 완료 출력에서 코드로 떼어낸다. 콜론을 동반한
# 줄머리 볼드만 손대므로 본문 강조 `**단어**`(콜론 없음)는 건드리지 않는다.
_SPEAKER_BOLD_RE = re.compile(
    r"^([ \t]*)\*\*\s*([^\n*]{1,20}?)\s*\*\*\s*:[ \t]*"  # **설하**:
    r"|^([ \t]*)\*\*\s*([^\n*]{1,20}?)\s*:\s*\*\*[ \t]*",  # **설하:**
    re.M,
)


def _strip_speaker_bold(text: str) -> str:
    """줄머리 볼드 화자 라벨(`**이름:**`·`**이름**:`)의 볼드를 떼 `이름: `로 정규화한다."""

    def _repl(m: "re.Match[str]") -> str:
        # 두 대안 중 매칭된 쪽의 (들여쓰기, 이름) 그룹을 골라 평문 라벨로 바꾼다.
        if m.group(2) is not None:
            return f"{m.group(1)}{m.group(2).strip()}: "
        return f"{m.group(3)}{m.group(4).strip()}: "

    return _SPEAKER_BOLD_RE.sub(_repl, text)


async def stream_chat_turn(messages: list[dict]) -> AsyncIterator[dict]:
    """messages를 LLM에 스트리밍 호출하고 token→completed(또는 error) 이벤트를 낸다.

    B안: `[다음 행동]` 마커 전까지의 본문만 token으로 흘리고, 마커부터는 흘리지 않는다
    (선택지는 completed의 choices로만 전달). 마커가 토큰 경계에 걸칠 수 있어, 끝
    (마커 길이-1)글자는 버퍼에 남겨 두고 나머지만 흘려 마커가 쪼개져도 놓치지 않는다.
    """
    full = ""          # 전체 누적 — 완료 시 본문/선택지 분리용
    pending = ""       # 아직 흘리지 않은 본문 버퍼 — 마커 prefix 겹침 보호
    body_done = False  # 마커를 만나 본문이 끝났는지
    model: str | None = None              # 응답이 돌려준 실제 모델(로깅 메타)
    input_tokens: int | None = None       # usage 청크에서 취득(없으면 None)
    output_tokens: int | None = None

    try:
        stream = await _client.chat.completions.create(
            model=settings.deepseek_chat_model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},  # 마지막 청크에 usage 동봉(토큰 로깅)
            extra_body=_THINKING_DISABLED,
        )
        async for chunk in stream:
            # 모델·usage는 choices가 빈 청크(특히 usage 전용 마지막 청크)에도 오므로
            # choices 가드보다 먼저 수집한다.
            model = getattr(chunk, "model", None) or model
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                # 토큰 필드 누락 시에도 'null 폴백' 계약을 지키도록 getattr로 방어한다.
                input_tokens = getattr(usage, "prompt_tokens", None)
                output_tokens = getattr(usage, "completion_tokens", None)
            # 메타데이터·필터 청크는 choices가 비어 올 수 있다 — 인덱싱 전에 가드.
            if not chunk.choices:
                continue
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
        ai_output = _strip_speaker_bold(ai_output)  # 화자 라벨의 볼드 제거(저장·표시값)
        # 로깅 메타 재료(model·토큰)를 함께 넘긴다 — 엔드포인트가 prompt_versions·provider를
        # 더해 completed의 meta로 조립한다(KNK-243).
        yield {
            "event": EVENT_COMPLETED,
            "ai_output": ai_output,
            "choices": choices,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    except OpenAIError as e:
        logger.exception("채팅 LLM 스트리밍 실패")  # 스택트레이스 포함 서버 로그
        # SSE는 HTTP 200이라 미들웨어가 못 잡는다 — 여기서 직접 Sentry로 보고한다(AN-4).
        capture_ai_exception(
            e,
            feature=FEATURE_CHAT_RESPONSE,
            model=settings.deepseek_chat_model,
            prompt_versions=LAYER_VERSIONS,
        )
        yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": str(e)}
