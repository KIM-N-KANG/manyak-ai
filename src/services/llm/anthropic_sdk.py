"""Anthropic SDK 어댑터 — 단발 호출(KNK-676).

두 번째 어댑터다. 첫 어댑터(`openai_sdk`)와 뼈대는 같고 하는 일도 같다 — 등록부의 **뜻**을
이 회사의 **문법**으로 옮기고, 응답을 공용 타입으로 되돌리고, SDK 예외를 중립 예외로 접는다.

**이 파일에는 모델 이름이 한 글자도 없다.** 어댑터는 SDK 계열 단위지 모델 단위가 아니다 —
모델 특성은 등록부가 적고 여기서는 그 뜻만 읽는다(사용자 원칙). 특정 모델에 맞춰 깎으면
모델을 바꿀 때마다 이 파일을 함께 고쳐야 한다.

OpenAI 계열과 다른 곳이 셋이다.

1. **system을 messages 밖으로 뺀다.** 이 회사는 지시문 칸이 요청 최상위에 따로 있다.
   칸이 하나뿐이라 맨 앞 것만 옮길 수 있다 — 나머지는 버린다(`_split_system` 참조).
2. **입력 토큰이 세 조각으로 온다.** 일반·캐시 생성·캐시 읽기를 더해야 전체 입력이 된다.
3. **본문이 블록 목록으로 온다.** 글 블록만 골라 이어 붙인다(추론 블록 등은 버린다).

스트리밍은 아직 없다 — KNK-696에서 만든다(`stream` 참조).
"""

import hashlib
import logging
from collections.abc import AsyncIterator

from anthropic import (
    NOT_GIVEN,
    AnthropicError,
    APITimeoutError,
    AsyncAnthropic,
    BadRequestError,
    RateLimitError,
)

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
    ResolvedModel,
    StreamEvent,
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

# 전송 실패 시 SDK가 다시 시도하는 횟수. **openai_sdk와 같은 2로 맞춘다** — 공급자를 바꿔도
# 호출부의 시간 계산이 같아야 한다. 시간 초과도 재시도 대상이라 예산이 부풀려지는 문제도
# 그대로 물려받는다(두 어댑터 공통 사안이라 별도 티켓).
_MAX_RETRIES = 2

# 이 어댑터는 아직 조각 흘리기를 못 한다 — KNK-696에서 True가 된다.
#
# 이 값 하나로 기동 검사가 막는다. 없으면 이 어댑터의 모델을 채팅에 꽂아도 서버가 정상으로
# 뜨고, 사용자가 채팅을 눌러야 터진다. 그것도 조용히 터진다 — 아래 `stream`이 던지는
# LlmConfigError는 채팅이 잡는 예외 계열(LlmError)이 아니라 SSE 오류 이벤트조차 못 나가고
# 스트림이 끊긴다(KNK-676 리뷰 P2, 재현 확인).
SUPPORTS_STREAMING = False


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


def _split_system(messages: list[Message], model: str) -> tuple[list[dict] | None, list[Message]]:
    """대화 목록에서 맨 앞 지시문(system)을 떼어 별도 칸으로 올린다.

    OpenAI 계열은 지시문도 대화 한 줄로 목록 안에 들어가지만, 이 회사는 요청 최상위에
    지시문 칸이 따로 있고 **그 칸은 하나뿐**이다.

    **맨 앞이 아닌 자리의 지시문은 버린다**(사용자 결정). 옮길 자리가 없어서다. 세 선택지
    (버린다 / user로 낮춘다 / 오류를 낸다) 중 버리는 쪽을 골랐다 — user로 낮추면 지시가
    사용자 발화와 같은 층위가 되고, 오류를 내면 채팅이 이 공급자로 아예 못 간다.

    **감수하는 위험**: 여기 걸리는 것은 채팅 본문뿐이다. 채팅은 지시문을 앞에 1개, 뒤에 2개
    (Depth·PHI) 놓는데, 뒤의 둘 중 PHI는 안전 가드레일이다. 즉 `CHAT_MODEL`을 이 어댑터를
    쓰는 모델로 돌리면 **서버도 채팅도 정상으로 동작하는데 안전 지시만 빠진 채 호출된다.**
    오류가 나지 않으므로 아래 경고 로그를 보지 않으면 알아채기 어렵다. 지금은 이 공급자를
    쓰는 모델이 등록부에 하나도 없어 닿을 길이 없고, 채팅을 여는 티켓이 배치 문제를 먼저
    풀어야 한다. 조용히 버리지 않는 것이 그때까지의 안전장치다.

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
            dropped += 1
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
            "하나뿐이라 옮길 자리가 없다. 채팅 본문이 뒤에 두는 Depth·PHI(안전 가드레일)가 "
            "여기 해당한다(KNK-676에서 감수한 위험).",
            model,
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


def _build_kwargs(req: LlmRequest, resolved: ResolvedModel) -> dict[str, object]:
    """SDK에 넘길 인자를 조립한다. 값이 없는 인자는 넣지 않는다(SDK 기본값에 맡긴다)."""
    system, messages = _split_system(req.messages, resolved.model)
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
    if req.json_mode:
        # 이 회사에는 "아무 JSON이나 좋으니 JSON으로만 답해"라는 스위치가 없다. 구조화 출력은
        # **JSON 스키마 전체**를 요구하는데(`output_config.format`의 schema가 필수), 통로가
        # 나르는 요청에는 그것을 담을 칸이 없다 — `LlmRequest.json_mode`는 켬/끔 불리언이다.
        #
        # 그래서 temperature와 같은 규칙으로 인자를 뺀다. 뺀다고 거짓이 되는 것은 없다 —
        # 프롬프트 템플릿 자체가 "JSON만 출력하라"고 지시하고 있고, 지키지 않았을 때의 대처도
        # 이미 호출부에 있다(스토리라인 재호출 KNK-312, 선택지 폴백, 판정 null).
        # 제대로 지원하려면 요청에 스키마 칸을 신설하고 호출부 세 곳이 각자 채워야 한다 —
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


def _usage_of(payload: object) -> TokenUsage:
    """usage를 옮긴다. **입력 토큰 세 조각을 합쳐야 전체 입력이 된다.**

    이 회사는 입력을 일반(`input_tokens`)·캐시 생성·캐시 읽기로 나눠 준다. 합계로 주는
    공급자(DeepSeek·GPT)와 달라서, 합치지 않으면 캐시가 잘 걸릴수록 실제보다 작은 값이
    백엔드에 적재된다. 조각 두 개는 내역으로도 남긴다.

    셋 다 없으면 0이 아니라 None이다(백엔드 계약: 누락 시 null).

    **`isinstance(값, int)`로 거르지 않는다.** 파이썬에서 `True`는 `int`의 하위 타입이라
    그 검사를 통과한다 — 깨진 응답이 `output_tokens=True`를 주면 백엔드에 숫자가 아니라
    JSON `true`가 실려 나가고, 합산에서는 `True`가 1로 세어진다(KNK-676 리뷰 P3, 재현 확인).
    `type(값) is int`로 정확히 정수만 받는다.
    """
    usage = getattr(payload, "usage", None)
    plain = _int_or_none(getattr(usage, "input_tokens", None))
    creation = _int_or_none(getattr(usage, "cache_creation_input_tokens", None))
    read = _int_or_none(getattr(usage, "cache_read_input_tokens", None))
    output = _int_or_none(getattr(usage, "output_tokens", None))
    parts = [value for value in (plain, creation, read) if value is not None]
    return TokenUsage(
        input_tokens=sum(parts) if parts else None,
        output_tokens=output,
        cache_creation_input_tokens=creation,
        cache_read_input_tokens=read,
    )


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
    하는 경로(선택지 폴백·판정 null)가 흡수에 실패해 턴이 500으로 깨진다. 인증·권한·404·
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
    아니라 번역되지 않고, 실패를 흡수해야 하는 경로(선택지 폴백·판정 null)를 관통해 턴이
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
    if req.max_tokens is None:
        raise LlmConfigError(
            f"모델 '{resolved.model}'은 Anthropic 어댑터를 쓰는데 단발 호출에 max_tokens가 "
            f"없습니다 — 이 공급자는 이 값으로 응답 대기 시간을 계산하므로 비워둘 수 없습니다."
        )
    client = _client(resolved.provider)
    try:
        response = await client.messages.create(**_build_kwargs(req, resolved))
    except AnthropicError as exc:
        raise _translate(exc, resolved) from exc
    return LlmResult(
        text=_text_of(response),
        # 응답이 돌려준 실제 모델명. 비어 오면 요청에 쓴 이름으로 채운다 — 빈 값을 올리면
        # 응답 meta 조립에서 터진다.
        model=getattr(response, "model", None) or req.model,
        provider=resolved.provider,
        usage=_usage_of(response),
        finish_reason=_finish_reason_of(response),
    )


def stream(req: LlmRequest, resolved: ResolvedModel) -> AsyncIterator[StreamEvent]:
    """스트리밍은 아직 없다 — KNK-696에서 만든다. 부르면 바로 LlmConfigError.

    함수를 아예 두지 않으면 통로가 이 모듈을 부르는 순간 `AttributeError`가 나는데, 그
    메시지로는 무엇이 없는지 알 수 없다(`base.LlmAdapter`가 경고한 그 상황이다). 무엇이
    없고 어느 티켓에서 생기는지 말해주는 쪽이 낫다.

    `async def`가 아니라 평범한 함수인 것은 의도다 — 통로의 `stream`은 결과를 그대로
    돌려주므로(await하지 않음), 여기가 async generator면 오류가 첫 조각을 꺼낼 때까지
    미뤄진다. 설정 실수는 호출한 자리에서 바로 드러나야 한다.
    """
    raise LlmConfigError(
        f"모델 '{resolved.model}'의 어댑터에는 조각 흘리기(스트리밍)가 아직 없습니다(KNK-696)."
    )
