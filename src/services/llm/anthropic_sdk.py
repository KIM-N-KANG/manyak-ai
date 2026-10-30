"""Anthropic SDK 어댑터 — 단발 호출(KNK-676)·조각 흘리기(KNK-696).

두 번째 어댑터다. 첫 어댑터(`openai_sdk`)와 뼈대는 같고 하는 일도 같다 — 등록부의 **뜻**을
이 회사의 **문법**으로 옮기고, 응답을 공용 타입으로 되돌리고, SDK 예외를 중립 예외로 접는다.

**이 파일에는 모델 이름이 한 글자도 없다.** 어댑터는 SDK 계열 단위지 모델 단위가 아니다 —
모델 특성은 등록부가 적고 여기서는 그 뜻만 읽는다(사용자 원칙). 특정 모델에 맞춰 깎으면
모델을 바꿀 때마다 이 파일을 함께 고쳐야 한다.

OpenAI 계열과 다른 곳이 넷이다.

1. **system을 messages 밖으로 뺀다.** 이 회사는 지시문 칸이 요청 최상위에 따로 있다.
   칸이 하나뿐이라 맨 앞 것만 옮긴다. 나머지는 대화 중간 지시문을 받는 모델이면 그 자리에
   그대로 두고, 아니면 버린다(`_split_system` 참조).
2. **입력 토큰이 세 조각으로 온다.** 일반·캐시 생성·캐시 읽기를 더해야 전체 입력이 된다.
3. **본문이 블록 목록으로 온다.** 글 블록만 골라 이어 붙인다(추론 블록 등은 버린다).
4. **스트리밍에서 토큰이 두 이벤트에 나뉘어 온다.** 입력은 시작 신호에, 최종 출력은 끝
   신호에 실린다 — 한쪽만 읽으면 절반이 빈다(`_absorb_usage`·`stream` 참조).

Langfuse 기록도 이 파일이 직접 한다(KNK-1627). `langfuse.openai` 자동 계측은 openai SDK만 덮고,
바깥 자동 계측(`opentelemetry-instrumentation-anthropic`)은 사용자가 도중에 끊으면 기록을 닫지
않아 뺐다(ADR A13). 단발 호출은 `observe_generation` 블록으로, 스트리밍은 정상·오류·취소마다
직접 닫는 `start_stream_generation`으로 남긴다.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import nullcontext

import httpx
from anthropic import (
    NOT_GIVEN,
    AnthropicError,
    APITimeoutError,
    AsyncAnthropic,
    BadRequestError,
    RateLimitError,
)

from src.core.langfuse import observe_generation, start_stream_generation
from src.core.tracing import start_span
from src.services.llm.base import (
    LlmBadRequest,
    LlmConfigError,
    LlmError,
    LlmRateLimited,
    LlmRequest,
    LlmResult,
    LlmTimeout,
    LlmUnavailable,
    Message,
    message_has_images,
    ResolvedModel,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    TokenUsage,
)
# 모듈째 가져온다(`from ... import credentials` 아님) — 이유는 openai_sdk와 같다.
# 이름을 직접 묶어두면 테스트가 `registry.credentials`를 갈아끼워도 여기엔 반영되지 않아,
# 가짜를 세운 줄 알고 실제 주소로 호출이 나갈 수 있다.
from src.services.llm import registry

logger = logging.getLogger(__name__)

# 공급자별 클라이언트를 재사용한다. 이름표·축출 정책은 openai_sdk와 같다(그쪽 주석 참조) —
# 설정이 바뀌면 새 클라이언트가 만들어지고, 옛 것은 닫지 않고 버린다(프로덕션 안정화 후로 미룬 사안).
_clients: dict[tuple[str, str | None, str], AsyncAnthropic] = {}

# 요청에서 별도로 지정하지 않았을 때 SDK가 다시 시도하는 횟수.
# 단발 호출은 LlmRequest.max_retries로 이 기본값을 덮어쓸 수 있다.
_MAX_RETRIES = 2

# 이 어댑터는 조각 흘리기를 한다(`stream`, KNK-696). 기동 검사가 읽는다 — `base.LlmAdapter` 참조.
#
# 어댑터는 지원 기능만 밝히고 용도별 허용 여부는 등록부에서 정한다.
SUPPORTS_STREAMING = True

# Langfuse 관측 이름. 자동 계측이 붙이는 "OpenAI-generation", Gemini 어댑터의 "Gemini-generation"과
# 나란히 공급자를 드러낸다.
_OBSERVATION_NAME = "Anthropic-generation"


def _fingerprint(api_key: str) -> str:
    """캐시 이름표에 넣을 키 지문.

    openai_sdk에 같은 함수가 있지만 **일부러 복제한다.** 어댑터끼리 서로를 가져오면 한쪽을
    고칠 때 다른 쪽이 딸려 오고, 공급자 하나를 떼어내기도 어려워진다. 어댑터는 서로를 모른다.

    키 원문을 이름표로 쓰지 않는 이유: 이름표는 예외 메시지·디버그 로그에 통째로 딸려 나갈 수
    있어 원문을 넣으면 API 키가 그 경로로 샌다. 지문이면 "설정이 바뀌었는지" 판정에 충분하다.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _client(provider: str) -> AsyncAnthropic:
    """공급자의 클라이언트를 얻는다(없으면 만들어 캐시).

    빈 키를 여기서 막는 이유도 openai_sdk와 같다 — 그냥 두면 SDK가 자기 예외를 던지는데
    그것은 호출부가 아는 중립 예외가 아니고, 성질상으로도 전송 실패가 아니라 설정 오류다.

    timeout은 클라이언트에 박지 않는다 — 호출마다 다르다(`LlmRequest.timeout`).
    """
    creds = registry.credentials(provider)
    if not creds.api_key.strip():
        raise LlmConfigError(
            f"공급자 '{provider}' 호출에 필요한 {creds.api_key_env}가 비어 있습니다."
        )
    key = (provider, creds.base_url, _fingerprint(creds.api_key))
    client = _clients.get(key)
    if client is None:
        client = AsyncAnthropic(
            api_key=creds.api_key, base_url=creds.base_url, max_retries=_MAX_RETRIES
        )
        _clients[key] = client
    return client


def _split_system(
    messages: list[Message], resolved: ResolvedModel
) -> tuple[list[dict] | None, list[Message]]:
    """대화 목록에서 맨 앞 지시문(system)을 떼어 별도 칸으로 올린다.

    OpenAI 계열은 지시문도 대화 한 줄로 목록 안에 들어가지만, 이 회사는 요청 최상위에
    지시문 칸이 따로 있고 **그 칸은 하나뿐**이다.

    **맨 앞이 아닌 자리의 지시문은 버린다**(사용자 결정). 옮길 자리가 없어서다. 세 선택지
    (버린다 / user로 낮춘다 / 오류를 낸다) 중 버리는 쪽을 골랐다 — user로 낮추면 지시가
    사용자 발화와 같은 층위가 되고, 오류를 내면 채팅이 이 공급자로 아예 못 간다.

    지원하지 않는 모델에서는 중간·끝 지시문이 빠질 수 있으므로 경고 로그를 남긴다.
    호출부는 지시문 위치가 보존돼야 하는 요청에 맞는 모델을 선택해야 한다.

    **다만 "옮길 자리가 없다"는 것은 요청 최상위 칸에 대해서만 맞다.** 이 회사는 대화 목록
    안에 지시문 줄을 끼워 넣는 방법을 따로 지원한다 — 첫 줄이면 안 되고, 사용자 발화 뒤에
    와야 하며, 마지막이거나 뒤에 assistant 턴이 와야 한다. 모델마다 지원 여부가 달라
    지원하지 않는 모델에는 이 방법을 사용하지 않는다.

    **그 길을 지원하는 모델이면 버리지 않고 그 자리에 둔다**(`supports_mid_conversation_system`).
    Claude 5.5 세대가 공식 지원한다(beta 헤더 불필요). 위치 조건은 회사가 검사하고 어기면 400이라
    여기서 다시 검사하지 않는다.

    캐시 표시(`cache_control`)는 **마지막 블록에만** 붙인다 — 그 지점까지를 캐시 대상으로
    잡는다는 뜻이라, 앞 블록에 붙이면 뒤가 캐시에서 빠진다. 조건 없이 붙이는 이유는 이
    어댑터를 쓰는 호출부 네 곳의 지시문이 모두 고정 템플릿 원문이기 때문이다.

    **내용이 빈 지시문은 아예 넣지 않는다** — 아래 주석 참조.
    """
    leading: list[str] = []
    rest: list[Message] = []
    dropped = 0
    for message in messages:
        if message.get("role") != "system":
            rest.append(message)
            continue
        # 아직 다른 역할이 하나도 안 나왔으면 "맨 앞"이다.
        if rest:
            if not resolved.supports_mid_conversation_system:
                dropped += 1
                continue
            # 빈 지시문은 맨 앞과 같은 이유로 넣지 않는다(빈 글 블록은 400).
            if (message.get("content") or "").strip():
                rest.append({"role": "system", "content": message["content"]})
            continue
        # **내용이 빈 지시문은 넣지 않는다.** 이 공급자는 빈 글 블록을 거부하는데, 캐시 표시가
        # 붙으면 특히 그렇다. 그러면 "지시문이 비었다"는 사소한 입력이 400이 되고, 같은 입력을
        # 받아주는 OpenAI 경로와 동작이 갈린다(KNK-676 리뷰 P3). 빈 지시문은 보낼 내용이
        # 없다는 뜻이므로 빼는 것이 곧 원래 의도다 — 여기서 새로 정하는 규칙이 아니다.
        content = message.get("content") or ""
        if content.strip():
            leading.append(content)

    if dropped:
        logger.warning(
            "모델 %s: 맨 앞이 아닌 자리의 system 지시 %d개를 버렸다 — 이 공급자는 지시문 칸이 "
            "하나뿐이고 이 모델은 대화 중간 system 지시를 지원하지 않는다.",
            resolved.model,
            dropped,
        )
    if not leading:
        return None, rest
    blocks: list[dict] = [{"type": "text", "text": text} for text in leading]
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks, rest


def _thinking_of(resolved: ResolvedModel) -> dict[str, str]:
    """등록부의 추론 켬/끔을 이 회사 문법으로. **생략하지 않고 언제나 명시한다.**

    이 회사는 모델마다 추론 기본값이 다르다(켜짐인 모델도, 꺼짐인 모델도 있다). 생략하면
    등록부에 적어둔 값과 실제 호출이 어긋나는데 오류가 나지 않아 드러나지도 않는다.

    매번 새 dict를 만든다 — 등록부 값을 그대로 넘겨 제자리에서 고치면 그 모델의 설정이
    프로세스 내내 오염된다.

    알려진 한계: 추론을 끌 수 없는 모델(항상 켜짐)에 `disabled`를 보내면 400이다. 어댑터는
    모델을 모르므로 여기서 걸러낼 수 없다 — 그런 모델은 등록부에 `use_thinking=True`로 적는다.
    """
    return {"type": "adaptive"} if resolved.use_thinking else {"type": "disabled"}


def check_supported(resolved: ResolvedModel) -> None:
    """이 어댑터가 이 모델의 설정을 요청 인자로 표현할 수 있는지 확인한다. 못 하면 LlmConfigError.

    기동 검사가 부른다(`llm.validate_startup`). 실제 호출은 하지 않으므로 과금도 지연도 없다.

    지금은 등록부가 적는 뜻(추론 켬/끔·temperature 수용)을 이 회사 문법이 모두 표현할 수 있어
    걸러낼 것이 없다. 그래도 openai_sdk와 같은 자리에 같은 모양으로 둔다 — 표현 못 하는 뜻이
    생기는 날 여기가 그것을 막는 자리다.
    """
    _thinking_of(resolved)
    if (
        resolved.reasoning_effort is not None
        and resolved.supported_reasoning_efforts
        and resolved.reasoning_effort not in resolved.supported_reasoning_efforts
    ):
        raise LlmConfigError(
            f"모델 '{resolved.model}'의 추론 강도 '{resolved.reasoning_effort}'가 지원 목록에 "
            f"없습니다: {sorted(resolved.supported_reasoning_efforts)}"
        )


def _build_kwargs(req: LlmRequest, resolved: ResolvedModel) -> dict[str, object]:
    """SDK에 넘길 인자를 조립한다. 값이 없는 인자는 넣지 않는다(SDK 기본값에 맡긴다)."""
    if message_has_images(req.messages):
        # 이미지 조각은 OpenAI 호환 모양이라 이 회사 문법(`{"type": "image", "source": ...}`)으로
        # 옮겨야 하는데 아직 옮기지 않는다(KNK-1359는 OpenAI SDK 어댑터만). 등록부가 이 어댑터의
        # 모델에 이미지 지원 표시를 달면 통로 검사를 통과해 여기까지 오므로, 그대로 보내
        # 400을 받거나 글만 보고 답하는 대신 설정 오류로 막는다.
        raise LlmConfigError(
            f"모델 '{resolved.model}'의 어댑터는 이미지 조각을 아직 옮기지 못합니다."
        )
    system, messages = _split_system(req.messages, resolved)
    kwargs: dict[str, object] = {
        "model": resolved.model,
        "messages": messages,
        # max_tokens만 예외적으로 **키를 늘 넣는다.** 이 SDK는 서명상 필수라 키가 없으면
        # 네트워크에 나가기도 전에 TypeError다. 값이 없을 때는 SDK의 "값 없음" 표식을 넣어
        # 요청 본문에서 이 항목이 빠지게 한다.
        #
        # **단, 단발 호출에서는 이 표식이 통하지 않는다** — `complete`의 가드 참조.
        "max_tokens": req.max_tokens if req.max_tokens is not None else NOT_GIVEN,
        "thinking": _thinking_of(resolved),
    }
    # none은 "추론 끔"이라는 뜻이고 이 회사의 effort 값이 아니다 — 위 thinking으로 표현하고 effort는
    # 보내지 않는다(보내면 400). 끈 상태의 effort는 high 이하여야 하는데, 생략하면 기본값이라 안전하다.
    if resolved.reasoning_effort not in (None, "none"):
        kwargs["output_config"] = {"effort": resolved.reasoning_effort}
    if system is not None:
        kwargs["system"] = system
    if req.temperature is not None:
        if resolved.supports_temperature:
            kwargs["temperature"] = req.temperature
        else:
            # 안 받는 모델에 보내면 400이다. 몰래 다른 값으로 바꾸지 않고 인자를 뺀다 —
            # openai_sdk와 같은 규칙이다.
            logger.info(
                "모델 %s는 temperature를 받지 않아 인자를 뺐다(요청값 %s)",
                resolved.model,
                req.temperature,
            )
    if req.timeout is not None:
        kwargs["timeout"] = req.timeout
    if req.max_tokens is not None and resolved.max_output_tokens is not None:
        if req.max_tokens > resolved.max_output_tokens:
            raise LlmConfigError(
                f"모델 '{resolved.model}'의 max_tokens={req.max_tokens}이 최대 출력 "
                f"{resolved.max_output_tokens}을 넘습니다."
            )
    if req.json_mode:
        # 이 회사에는 "아무 JSON이나 좋으니 JSON으로만 답해"라는 스위치가 없다. 구조화 출력은
        # **JSON 스키마 전체**를 요구하는데(`output_config.format`의 schema가 필수), 통로가
        # 나르는 요청에는 그것을 담을 칸이 없다 — `LlmRequest.json_mode`는 켬/끔 불리언이다.
        #
        # 그래서 temperature와 같은 규칙으로 인자를 뺀다. 뺀다고 거짓이 되는 것은 없다 —
        # 프롬프트 템플릿 자체가 "JSON만 출력하라"고 지시하고 있고, 지키지 않았을 때의 대처도
        # 이미 호출부에 있다(스토리라인 재호출 KNK-312, 선택지 폴백).
        # 제대로 지원하려면 요청에 스키마 칸을 신설하고 호출부 두 곳이 각자 채워야 한다 —
        # 계약 변경이라 별도 티켓이다.
        logger.warning(
            "모델 %s: json_mode를 인자로 옮기지 못해 뺐다 — 이 공급자는 JSON 강제에 스키마를 "
            "요구하는데 요청에 담을 칸이 없다. 형식 준수는 프롬프트 지시에만 의존한다.",
            resolved.model,
        )
    return kwargs


def _text_of(response: object) -> str:
    """본문을 꺼낸다. **어떤 모양이 와도 예외를 던지지 않는다** — 못 꺼내면 빈 문자열이다.

    이 회사는 본문을 블록 목록으로 준다. 글 블록(`type == "text"`)만 골라 이어 붙이고
    나머지(추론 블록 등)는 버린다.

    규칙과 이유는 openai_sdk._text_of와 같다 — 붙잡은 것을 공급자 장애로 둔갑시키지 않는다.
    여기서 예외를 던지면 스토리라인 invalid 재호출(KNK-312)이 전송 오류 경로로 새서 사라진다.
    """
    try:
        parts = [
            block.text
            for block in response.content  # type: ignore[attr-defined]
            if getattr(block, "type", None) == "text"
            and isinstance(getattr(block, "text", None), str)
        ]
    except Exception:  # 계약이 "절대 던지지 않는다"이므로 꺼내기 실패는 전부 빈 본문으로 본다.
        logger.warning("응답에서 본문을 꺼내지 못했다 — 빈 본문으로 넘긴다", exc_info=True)
        return ""
    return "".join(parts)


def _int_or_none(value: object) -> int | None:
    """토큰 수로 쓸 수 있는 값이면 그대로, 아니면 None.

    `type(value) is int`인 것이 중요하다 — `isinstance`는 `True`/`False`도 통과시킨다.
    """
    return value if type(value) is int else None


# usage에서 읽는 칸들. 앞 셋이 입력, 마지막이 출력이다.
_INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_USAGE_FIELDS = (*_INPUT_FIELDS, "output_tokens")


def _usage_parts(payload: object) -> dict[str, int | None]:
    """payload의 usage에서 네 칸을 있는 그대로 꺼낸다(합산 전).

    **`isinstance(값, int)`로 거르지 않는다.** 파이썬에서 `True`는 `int`의 하위 타입이라
    그 검사를 통과한다 — 깨진 응답이 `output_tokens=True`를 주면 백엔드에 숫자가 아니라
    JSON `true`가 실려 나가고, 합산에서는 `True`가 1로 세어진다(KNK-676 리뷰 P3, 재현 확인).
    `_int_or_none`이 `type(값) is int`로 정확히 정수만 받는다.
    """
    usage = getattr(payload, "usage", None)
    return {name: _int_or_none(getattr(usage, name, None)) for name in _USAGE_FIELDS}


def _usage_from_parts(parts: dict[str, int | None]) -> TokenUsage:
    """꺼낸 네 칸을 공용 타입으로 접는다. **입력 세 조각을 합쳐야 전체 입력이 된다.**

    이 회사는 입력을 일반(`input_tokens`)·캐시 생성·캐시 읽기로 나눠 준다. 합계로 주는
    공급자(DeepSeek·GPT)와 달라서, 합치지 않으면 캐시가 잘 걸릴수록 실제보다 작은 값이
    백엔드에 적재된다. 조각 두 개는 내역으로도 남긴다.

    셋 다 없으면 0이 아니라 None이다(백엔드 계약: 누락 시 null).
    """
    inputs = [parts[name] for name in _INPUT_FIELDS if parts[name] is not None]
    return TokenUsage(
        input_tokens=sum(inputs) if inputs else None,
        output_tokens=parts["output_tokens"],
        cache_creation_input_tokens=parts["cache_creation_input_tokens"],
        cache_read_input_tokens=parts["cache_read_input_tokens"],
    )


def _usage_of(payload: object) -> TokenUsage:
    """단발 응답의 usage를 옮긴다 — 한 곳에 다 들어 있으므로 꺼내서 바로 접는다."""
    return _usage_from_parts(_usage_parts(payload))


def _absorb_usage(parts: dict[str, int | None], payload: object) -> None:
    """스트림 이벤트가 실어온 토큰 값을 누적표에 반영한다 — **값이 있는 칸만 덮어쓴다.**

    이 회사는 스트리밍에서 토큰을 **두 이벤트에 나눠** 보낸다. 입력은 시작 신호
    (`message_start`)에, 최종 출력은 끝 신호(`message_delta`)에 실린다. 둘 다 증가분이
    아니라 "지금까지의 총합"이라 나중 값으로 덮어쓰는 것이 맞다.

    없는 칸(None)으로 덮지 않는 것이 이 함수의 요점이다. 끝 신호의 usage는 입력 칸을
    비워 보내는 것이 보통이라(0.120.0의 `MessageDeltaUsage` 기본값이 전부 None), 그대로
    덮으면 시작 신호에서 받아둔 입력 토큰이 마지막에 지워진다.
    """
    for name, value in _usage_parts(payload).items():
        if value is not None:
            parts[name] = value


def _usage_details(parts: dict[str, int | None]) -> dict[str, int] | None:
    """누적한 토큰 네 칸을 Langfuse usage_details로 옮긴다(KNK-1627).

    키는 Langfuse 관리 단가(claude-sonnet-5 등)가 값을 매긴 이름에 맞춘다 — 이름이 어긋나면 그
    토큰은 0원으로 계산되고, 같은 토큰을 두 이름으로 보내면 두 번 계산된다. 그래서:

    - `input`은 **캐시를 뺀 일반 입력**(`input_tokens`)만이다. 이 회사는 입력을 세 조각으로 주고
      조각마다 단가가 달라(캐시 쓰기 1.25배, 캐시 읽기 0.1배), 합계를 `input`에 넣으면 캐시
      조각이 일반 단가로 한 번 더 계산된다. 내부 지표용 `TokenUsage`가 셋을 합치는 것과 다르다.
    - 캐시 두 조각은 각자 이름(`cache_creation_input_tokens`·`cache_read_input_tokens`)으로 싣는다.

    값이 하나도 없으면 None을 돌려준다.
    """
    mapping = {
        "input": parts["input_tokens"],
        "cache_creation_input_tokens": parts["cache_creation_input_tokens"],
        "cache_read_input_tokens": parts["cache_read_input_tokens"],
        "output": parts["output_tokens"],
    }
    details = {key: value for key, value in mapping.items() if value is not None}
    return details or None


def _unfinished_usage_details(
    parts: dict[str, int | None], told_it_finished: bool
) -> dict[str, int] | None:
    """도중에 끝난 스트림의 usage_details. 마무리 신호를 못 받았으면 출력 칸을 비운다.

    출력 토큰은 마무리 신호(`message_delta`)에서만 실제 값이 오고, 시작 신호의 값은 임시값(보통 1)
    이다. 끊긴 턴에 그 1을 실으면 실제로 쓴 분량과 상관없이 "거의 안 썼다"로 기록돼 비용이 적게
    잡힌다. 틀린 숫자보다 빈칸이 낫다 — 입력 조각은 시작 신호의 값이 실제 값이라 그대로 싣는다.
    """
    if told_it_finished:
        return _usage_details(parts)
    return _usage_details(parts | {"output_tokens": None})


def _model_parameters(
    req: LlmRequest, resolved: ResolvedModel
) -> dict[str, str | int | float | bool | None]:
    """관측에 실을 모델 인자. `_build_kwargs`가 실제로 보내는 값만 요약한다."""
    params: dict[str, str | int | float | bool | None] = {
        "thinking": _thinking_of(resolved)["type"],
    }
    if req.max_tokens is not None:
        params["max_tokens"] = req.max_tokens
    if resolved.reasoning_effort not in (None, "none"):
        params["effort"] = resolved.reasoning_effort
    if req.temperature is not None and resolved.supports_temperature:
        params["temperature"] = req.temperature
    return params


def _finish_reason_of(payload: object) -> str | None:
    """응답 종료 이유. `_text_of`와 같은 규칙 — 못 꺼내면 None이고 예외는 없다.

    **값의 어휘가 회사마다 다르다.** 길이 상한에 걸렸을 때 OpenAI 계열은 `"length"`,
    이 회사는 `"max_tokens"`다. 지금 이 값을 읽어 판정하는 코드가 없어 문제되지 않지만,
    잘림 감지를 붙이는 날 두 어휘를 함께 봐야 한다.
    """
    try:
        reason = payload.stop_reason  # type: ignore[attr-defined]
    except Exception:
        logger.warning("응답에서 종료 이유를 꺼내지 못했다 — None으로 넘긴다", exc_info=True)
        return None
    return reason if isinstance(reason, str) else None


def _translate(exc: AnthropicError, resolved: ResolvedModel) -> LlmError:
    """SDK 예외를 공급자 중립 예외로 접는다.

    **순서가 중요하다.** 이 SDK도 `APITimeoutError`가 `APIConnectionError`의 하위 클래스다
    (0.120.0에서 직접 확인). 연결 오류를 먼저 검사하면 모든 타임아웃이 unavailable로 분류돼
    error_code와 사용자 문구까지 바뀐다.

    마지막은 catch-all이다 — 번역되지 않은 SDK 예외가 호출부까지 올라가면 실패를 흡수해야
    하는 경로(선택지 폴백)가 흡수에 실패해 턴이 500으로 깨진다. 인증·권한·404·
    422·5xx는 4xx 여부와 무관하게 여기로 묶인다(openai_sdk와 같은 경계).
    """
    context = {"provider": resolved.provider, "model": resolved.model}
    if isinstance(exc, APITimeoutError):
        return LlmTimeout(str(exc), **context)
    if isinstance(exc, RateLimitError):
        return LlmRateLimited(str(exc), **context)
    if isinstance(exc, BadRequestError):
        return LlmBadRequest(str(exc), **context)
    return LlmUnavailable(str(exc), **context)


async def complete(req: LlmRequest, resolved: ResolvedModel) -> LlmResult:
    """단발 호출. 재호출·시간 예산은 호출부가 관장하고 여기서는 한 번만 부른다.

    **이 공급자의 단발 호출에는 max_tokens가 반드시 있어야 한다.** SDK가 이 조건에서
    응답 대기 시간을 max_tokens로 계산하기 때문이다(0.120.0 소스 확인):

        if not stream and not is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT:
            timeout = self._client._calculate_nonstreaming_timeout(max_tokens, ...)
            # 안에서 max_tokens를 산술에 쓴다

    비워두면 "값 없음" 표식이 그 계산에 들어가 `TypeError`가 난다. 그 예외는 SDK 예외가
    아니라 번역되지 않고, 실패를 흡수해야 하는 경로(선택지 폴백)를 관통해 턴이
    500으로 깨진다. 조건에 `not stream`이 있어 **스트리밍에는 해당하지 않는다** — 그래서
    가드를 `_build_kwargs`가 아니라 여기 둔다(KNK-696이 그 표식을 그대로 쓸 수 있게).

    **가드는 위 조건보다 일부러 엄격하다.** 조건에는 "timeout을 안 줬을 때"도 들어 있어,
    timeout이 있으면 max_tokens가 없어도 지금은 터지지 않는다. 그래도 막는 이유는 규칙이
    "이 어댑터의 단발 호출에는 max_tokens가 필요하다" 하나여야 읽는 사람이 헷갈리지 않고,
    무관해 보이는 다른 필드에 따라 되고 안 되고가 갈리지 않기 때문이다. 지금 이 경로를 쓰는
    호출부 셋은 모두 두 값을 다 채운다.

    LlmError가 아니라 LlmConfigError인 것은 의도다. 이건 공급자 장애가 아니라 호출부가 값을
    빠뜨린 우리 쪽 결함이라, 502로 접히면 공급자 장애로 위장된다(STYLEGUIDE §4).
    """
    with start_span(
        "llm.complete", {"provider": resolved.provider, "model": resolved.model, "operation": "complete"},
    ):
        if req.max_tokens is None:
            raise LlmConfigError(
                f"모델 '{resolved.model}'은 Anthropic 어댑터를 쓰는데 단발 호출에 max_tokens가 "
                f"없습니다 — 이 공급자는 이 값으로 응답 대기 시간을 계산하므로 비워둘 수 없습니다."
            )
        client = _client(resolved.provider)
        if req.max_retries is not None:
            client = client.with_options(max_retries=req.max_retries)
        kwargs = _build_kwargs(req, resolved)
        # 관측 블록 안에서 나가는 예외(공급자 실패)는 관측에 ERROR로 남고 그대로 전파된다.
        observation = observe_generation(
            _OBSERVATION_NAME,
            model=resolved.model,
            model_parameters=_model_parameters(req, resolved),
            input_data=req.messages,
        ) if req.automatic_observation else nullcontext()
        with observation as generation:
            try:
                response = await client.messages.create(**kwargs)
            except AnthropicError as exc:
                raise _translate(exc, resolved) from exc
            text = _text_of(response)
            if generation is not None:
                generation.finish(output=text, usage_details=_usage_details(_usage_parts(response)))
        return LlmResult(
            text=text,
            # 응답이 돌려준 실제 모델명. 비어 오면 요청에 쓴 이름으로 채운다 — 빈 값을 올리면
            # 응답 meta 조립에서 터진다.
            model=getattr(response, "model", None) or req.model,
            provider=resolved.provider,
            usage=_usage_of(response),
            finish_reason=_finish_reason_of(response),
        )


async def stream(req: LlmRequest, resolved: ResolvedModel) -> AsyncIterator[StreamEvent]:
    """스트리밍 호출. 조각을 TextDelta로 흘리고 마지막에 StreamCompleted를 낸다(KNK-696).

    **이 회사는 응답을 여러 종류의 신호로 나눠 보낸다.** OpenAI 계열은 신호가 한 종류
    (청크)뿐이라 매번 같은 자리를 읽으면 됐지만, 여기서는 신호마다 실려 오는 것이 다르다.

    - `message_start` — 모델 이름과 **입력** 토큰
    - `content_block_delta` — 실제 조각. 그중 `text_delta`만 글이다
    - `message_delta` — 종료 이유와 **최종 출력** 토큰
    - 그 밖(`content_block_start`·`content_block_stop`·`message_stop`) — 읽을 것이 없다

    토큰이 두 신호에 나뉘어 오는 것이 함정이다. 한쪽만 읽으면 입력이나 출력 한쪽이 빈 채로
    백엔드에 적재된다 — 오류가 나지 않아 드러나지도 않는다(`_absorb_usage` 참조).

    **마무리 신호(`message_delta`) 없이 끝나면 오류로 본다.** 반복이 끝났다는 것만으로는
    답이 끝났다는 뜻이 아니다 — 아래 잘림 판정 참조.

    추론 조각(`thinking_delta`)과 서명(`signature_delta`)은 버린다. 그대로 흘리면 모델의
    생각 과정이 사용자 화면에 그대로 뜬다.

    **max_tokens 가드를 여기에는 두지 않는다.** 단발 호출과 달리 SDK가 이 값으로 대기 시간을
    계산하지 않아(그 조건에 `not stream`이 있다) 번역 불가 TypeError가 나지 않는다.

    다만 "없어도 된다"는 뜻은 아니다. **max_tokens는 이 회사 요청의 필수 항목이라, 빠진 채로
    나가면 거부당한다(400).** 이 SDK가 스트리밍 오버로드에서도 필수로 선언하는 것과 같은
    이유다(0.120.0 서명 확인). 채팅은 이 값을 싣는다(`chat_llm._MAX_TOKENS`, KNK-1624). 이 회사의
    최신 모델은 추론을 켜면 이 한도가 추론과 본문을 합쳐서 자르므로, 그 값을 넉넉하게 잡았다.

    그래도 여기서 막지 않는 이유는, 막으면 LlmConfigError가 되어 채팅이 잡는 예외 계열을
    벗어나 SSE 오류 이벤트도 없이 끊기기 때문이다. 공급자가 400을 주면 LlmBadRequest로 접혀
    오류 이벤트가 정상으로 나간다.

    사용자 연결 취소(`CancelledError`)는 여기서 잡지 않는다 — BaseException이라 아래
    except들에 걸리지 않고, 취소는 오류가 아니라 그냥 스트림이 끊기는 것이다.
    """
    with start_span(
        "llm.stream", {"provider": resolved.provider, "model": resolved.model, "operation": "stream"},
    ):
        client = _client(resolved.provider)
        kwargs = _build_kwargs(req, resolved)
        kwargs["stream"] = True

        model = req.model
        parts: dict[str, int | None] = dict.fromkeys(_USAGE_FIELDS, None)
        finish_reason: str | None = None
        told_it_finished = False  # 마무리 신호(`message_delta`)를 받았는지 — 아래 잘림 판정에 쓴다
        texts: list[str] = []  # Langfuse 관측의 출력. 취소돼도 그때까지 흘린 글을 남긴다
        # 관측은 호출 전에 연다 — 요청 단계에서 실패해도 그 실패가 기록에 남아야 한다.
        # 어떻게 끝나든 아래 세 갈래 중 하나가 반드시 닫는다(KNK-1627, ADR A13).
        generation = start_stream_generation(
            _OBSERVATION_NAME,
            model=resolved.model,
            model_parameters=_model_parameters(req, resolved),
            input_data=req.messages,
        )
        try:
            try:
                events = await client.messages.create(**kwargs)
            except AnthropicError as exc:
                raise _translate(exc, resolved) from exc

            try:
                async for event in events:
                    kind = getattr(event, "type", None)
                    if kind == "message_start":
                        # 모델 이름과 입력 토큰은 이 신호에만 온다.
                        message = getattr(event, "message", None)
                        model = getattr(message, "model", None) or model
                        _absorb_usage(parts, message)
                    elif kind == "message_delta":
                        told_it_finished = True
                        _absorb_usage(parts, event)
                        # 종료 이유는 이벤트가 아니라 그 안의 delta에 있다.
                        reason = getattr(getattr(event, "delta", None), "stop_reason", None)
                        if isinstance(reason, str) and reason:
                            finish_reason = reason
                    elif kind == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) != "text_delta":
                            continue  # 추론·서명 조각. 사용자에게 보낼 글이 아니다
                        # 글자인지 확인하고 쓴다 — `_text_of`와 같은 규칙이다. 이상한 값을 그대로
                        # 흘리면 사용자 화면에 그 표기가 뜨거나, 이어 붙이는 쪽에서 터진다.
                        text = getattr(delta, "text", None)
                        if isinstance(text, str) and text:
                            texts.append(text)
                            yield TextDelta(text)
                if not told_it_finished:
                    # **끝났다는 말을 못 듣고 스트림이 닫혔다 — 정상 종료가 아니다.**
                    # 이 회사는 답을 마무리할 때 반드시 `message_delta`를 보낸다. 그것 없이 연결이
                    # 조용히 닫히는 경우가 있는데(중간 프록시가 본문을 자르면서 정상 종료로 닫는 경우),
                    # SDK는 예외를 내지 않는다. 그대로 두면 **잘린 본문이 완성된 답으로 저장되고**
                    # 출력 토큰도 시작 시점 값(보통 1)으로 굳는다 — 오류가 없으니 아무도 모른다
                    # (KNK-696 리뷰 P1, 실제 SDK + 가짜 전송으로 재현 확인).
                    #
                    # 판정 기준을 **신호가 왔는지**로 두고 종료 이유 값으로 두지 않는다. 값이 이상하게
                    # 온 것("끝났다고는 하는데 알아볼 수 없는 말")과 아예 못 들은 것은 다른 상황이다.
                    # 앞은 meta를 비우고 넘기면 되지만, 뒤는 답이 잘렸다는 뜻이라 넘기면 안 된다.
                    raise LlmUnavailable(
                        "응답이 끝나기 전에 스트림이 닫혔습니다(종료 신호 없음).",
                        provider=resolved.provider,
                        model=resolved.model,
                    )
            except AnthropicError as exc:
                # 일부를 이미 흘려보낸 뒤여도 중립 예외로 바꿔 던진다 — 호출부가 SSE error 이벤트로 옮긴다.
                # 스트림 도중의 `error` 신호도 여기 걸린다(SDK가 그 신호를 자기 예외로 바꿔 올린다).
                raise _translate(exc, resolved) from exc
            except httpx.TimeoutException as exc:
                # 스트림이 열린 뒤의 읽기 타임아웃. SDK는 요청 단계의 타임아웃만 APITimeoutError로 접고
                # 반복 중의 것은 그대로 올려보낸다 — 아래 분기에 맡기면 같은 시간 초과가 발생 시점에 따라
                # provider_timeout / provider_unavailable로 갈린다.
                raise LlmTimeout(
                    f"{type(exc).__name__}: {exc}", provider=resolved.provider, model=resolved.model
                ) from exc
            except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                # SDK 밖으로 새는 **전송·파싱 오류만** 접는다. 이 SDK의 스트림 반복도 try/finally로만
                # 감싸져(except 없음) 연결 끊김(httpx)·깨진 SSE 줄(json)이 번역 없이 통과한다.
                #
                # `UnicodeDecodeError`가 따로 필요하다 — 이 SDK는 SSE 원문 바이트를 자기가 UTF-8로
                # 푸는데, 중간에 바이트가 깨지면 이 예외가 그대로 새어 나온다. 위 둘 중 어느 계열도
                # 아니라 번역되지 않고, 채팅은 오류 이벤트도 못 내고 끊긴다(KNK-696 리뷰 P1, 재현 확인).
                #
                # 모든 예외를 잡지 않는 이유: 우리 코드의 결함(오타·형 실수)까지 접으면 그것이
                # "공급자 장애"로 기록돼 원인 추적이 헛돈다(STYLEGUIDE §4).
                raise LlmUnavailable(
                    f"{type(exc).__name__}: {exc}", provider=resolved.provider, model=resolved.model
                ) from exc
            finally:
                # 스트림을 명시적으로 닫아 커넥션을 바로 반납한다. SDK도 자체 finally에서 닫지만 그것은
                # 내부 제너레이터가 정리될 때(GC 시점) 실행돼, 사용자가 채팅 도중 창을 닫으면 반납이
                # 늦어진다 — 흔한 경로라 여기서 결정적으로 닫는다.
                try:
                    await events.close()
                except Exception:  # 정리 실패가 원래 오류·취소를 덮으면 안 된다 — 삼키되 로그로 남긴다.
                    logger.warning("스트림 정리에 실패했다 — 원래 결과를 그대로 둔다", exc_info=True)
        except (GeneratorExit, asyncio.CancelledError):
            # 사용자가 답변 도중 창을 닫았다. 오류가 아니라 취소로 닫고 그때까지의 토큰을 싣는다.
            # 바깥 자동 계측을 뺀 이유가 바로 이 경로에서 기록을 닫지 않아서였다(ADR A13).
            generation.cancel(
                output="".join(texts), usage_details=_unfinished_usage_details(parts, told_it_finished)
            )
            raise
        except BaseException as exc:
            generation.fail(exc, usage_details=_unfinished_usage_details(parts, told_it_finished))
            raise
        generation.finish(output="".join(texts), usage_details=_usage_details(parts))
        yield StreamCompleted(
            model=model,
            provider=resolved.provider,
            usage=_usage_from_parts(parts),
            finish_reason=finish_reason,
        )
