"""채팅 본문 LLM 통로(ChatProvider) — 스트리밍 호출 (시점 B).

`assemble()`가 만든 messages를 DeepSeek(OpenAI 호환) `stream=True`로 호출하고,
토큰을 실시간으로 흘린다. 본문은 상황 묘사 + 인물 대사만 만든다 — **다음 행동 선택지는
이 호출이 만들지 않으며**, 본문 스트림이 끝난 뒤 별도 호출(`chat_choices`)이 전담한다
(그래서 `[다음 행동]` 마커 파싱·버퍼링이 사라졌다).

이벤트(dict)를 async generator로 낸다. SSE 와이어 변환·선택지 합치기는 엔드포인트(chat.py)가 맡는다.
- {"event": "token",     "text": ...}
- {"event": "completed", "ai_output": ..., "model": ..., "provider": ..., "input_tokens": ..., "output_tokens": ...}
- {"event": "error",     "code": ..., "message": ...}
"""

import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import aclosing

from src.core.config import settings
from src.core.sentry import FEATURE_CHAT_RESPONSE, capture_ai_exception
from src.schemas.chat_turn import EVENT_COMPLETED, EVENT_ERROR, EVENT_TOKEN
from src.services import llm
from src.services.chat_assembler import LAYER_VERSIONS
from src.services.llm.base import LlmError, LlmRequest, TextDelta

logger = logging.getLogger(__name__)

# LLM 호출은 공통 통로(src.services.llm)를 통한다(KNK-673). 어느 회사 SDK로 어떤 인자를
# 보낼지는 모델 등록부와 어댑터가 정하므로, 여기서는 "무엇을 원하는지"만 넘긴다 —
# 클라이언트 생성·추론 모드(thinking) 같은 회사 문법은 이 파일에서 사라졌다.

# 이 호출의 제한 시간(초). 첫 토큰까지의 상한이고, 스트리밍이라 통상 빠르게 시작된다.
# **호출마다 반드시 넘긴다** — 비우면 상한이 SDK 기본값(10분)으로 늘어난다.
_TIMEOUT_SECONDS = 90.0


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

    본문은 선택지 없이 지문·대사만 생성하므로, 받은 델타를 가공 없이 그대로 흘린다.
    완료 시 누적 본문에서 줄머리 볼드 화자 라벨만 정규화해 `ai_output`으로 낸다 — 선택지는
    여기서 만들지 않으며, 엔드포인트가 본문 종료 후 별도 호출로 받아 completed에 합친다.
    """
    full = ""                              # 전체 누적 — 완료 시 ai_output
    model: str | None = None               # 응답이 돌려준 실제 모델(로깅 메타)
    input_tokens: int | None = None        # 종료 이벤트에서 취득(없으면 None)
    output_tokens: int | None = None
    # 이 호출이 어느 공급자로 갈지는 부르기 전에 정해진다 — 스트림이 오류로 끝나면 종료
    # 이벤트가 아예 없어서, 결과에서 읽는 방식으로는 실패 태그를 채울 수 없다(KNK-674).
    provider = llm.provider_of(settings.chat_model)
    start = time.monotonic()

    try:
        # 조각을 흘리고 마지막에 종료 이벤트(모델·토큰)를 준다. 빈 청크 가드·usage 수집·
        # 회사별 청크 모양 해석은 통로가 맡는다.
        #
        # `aclosing`으로 감싸는 이유: 사용자가 채팅 도중 창을 닫으면 이 제너레이터가 중간에
        # 닫히는데, `async for`는 중도 이탈 때 **안쪽 제너레이터를 닫아주지 않는다.** 그러면
        # 어댑터가 커넥션을 반납하려고 넣어둔 정리 코드(`openai_sdk.stream`의 finally)가
        # 쓰레기 수집 시점까지 밀린다 — 어댑터에 있는 결정적 정리가 실제 운영 경로에서만
        # 작동하지 않는 상태가 된다(KNK-673 리뷰에서 탐침으로 확인).
        async with aclosing(
            llm.stream(
                LlmRequest(
                    model=settings.chat_model,
                    messages=messages,
                    timeout=_TIMEOUT_SECONDS,
                )
            )
        ) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    full += event.text
                    yield {"event": EVENT_TOKEN, "text": event.text}
                else:  # StreamCompleted — 정상 종료일 때만 온다
                    model = event.model
                    input_tokens = event.usage.input_tokens
                    output_tokens = event.usage.output_tokens

        ai_output = _strip_speaker_bold(full.strip())  # 화자 라벨의 볼드 제거(저장·표시값)
        # 로깅 메타 재료(model·토큰)를 함께 넘긴다 — 엔드포인트가 판정 호출 메타를 합산하고
        # prompt_versions·provider를 더해 completed의 meta로 조립한다(KNK-243).
        # 선택지 몫 메타는 KNK-625로 /chat/choices 응답 meta에 분리됐다.
        yield {
            "event": EVENT_COMPLETED,
            "ai_output": ai_output,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "provider": provider,
        }
    except LlmError as e:
        # 전송 오류(타임아웃·429·요청거부·연결실패)를 통로가 공급자 중립 예외로 접어 준 것.
        # 일부 토큰을 이미 흘려보낸 뒤여도 여기로 와서 error 이벤트로 끝난다.
        logger.exception("채팅 본문 LLM 스트리밍 실패")  # 스택트레이스 포함 서버 로그
        # SSE는 HTTP 200이라 미들웨어가 못 잡는다 — 여기서 직접 Sentry로 보고한다(AN-4).
        capture_ai_exception(
            e,
            feature=FEATURE_CHAT_RESPONSE,
            provider=provider,
            model=settings.chat_model,
            prompt_versions=LAYER_VERSIONS,
            retry_count=0,  # 본문은 재호출 없음
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        # 내부 상세(str(e))는 Sentry·로그로만 남기고 클라이언트엔 정제 메시지를 보낸다(AN-4-10).
        yield {
            "event": EVENT_ERROR,
            "code": "LLM_ERROR",
            "message": "채팅 연동 중 오류가 발생했습니다.",
        }
