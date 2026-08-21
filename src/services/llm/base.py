"""LLM 통로의 공용 타입 — 요청·결과·스트림 이벤트·공급자 중립 예외(KNK-670).

호출부와 어댑터가 주고받는 말을 여기서 정한다. 호출부는 회사별 SDK 타입을 모르고,
어댑터는 호출부의 도메인을 모른다.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, TypeAlias

# 어댑터 종류 = SDK 계열이다(회사 단위가 아니다). DeepSeek과 GPT는 같은 OpenAI SDK를 쓰고
# base_url·키만 다르므로 어댑터 하나를 공유한다. Anthropic은 SDK가 달라 별 어댑터를 쓴다(KNK-675).
# Google은 자체 SDK(google-genai)를 쓴다(KNK-951).
ADAPTER_OPENAI_SDK = "openai_sdk"
ADAPTER_ANTHROPIC_SDK = "anthropic_sdk"
ADAPTER_GOOGLE_SDK = "google_sdk"

# 공급자 식별자 — 로깅 메타(meta.provider)와 Sentry provider 태그(AN-4-8)에 그대로 실린다.
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GOOGLE = "google"

# 구조화 출력 능력. 요청 문법은 어댑터가 만들고, 등록부에는 모델이 받아들이는 방식만 적는다.
STRUCTURED_OUTPUT_JSON_OBJECT = "json_object"
STRUCTURED_OUTPUT_JSON_SCHEMA = "json_schema"

# LLM에 보내는 대화 한 줄. role은 소문자("system"·"user"·"assistant") — OpenAI 호환 규약.
Message: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class ModelPricing:
    """공급자 직결 표준 API의 USD 단가(토큰 100만 개 기준).

    배치·리전 고정·서버 도구처럼 호출 옵션에 따라 붙는 별도 요금은 포함하지 않는다. 캐시 쓰기
    단가는 공급자가 별도로 과금하고 현재 어댑터가 그 종류를 쓰는 경우에만 적는다. 예를 들어
    Anthropic 어댑터의 ``ephemeral`` 캐시는 기본 5분이라 5분 쓰기 단가를 쓴다.
    """

    input_usd_per_1m_tokens: Decimal
    cache_read_input_usd_per_1m_tokens: Decimal
    output_usd_per_1m_tokens: Decimal
    source_url: str
    verified_on: date
    effective_from: date | None = None
    effective_until: date | None = None
    cache_write_input_usd_per_1m_tokens: Decimal | None = None
    # GPT-5.6은 입력이 이 값을 넘으면 요청 전체의 입력·출력 단가가 각각 할증된다.
    long_context_threshold_tokens: int | None = None
    long_context_input_multiplier: Decimal = Decimal("1")
    long_context_output_multiplier: Decimal = Decimal("1")

    def applies_on(self, on: date) -> bool:
        """이 단가가 지정 날짜에 적용되는지 반환한다."""
        return (self.effective_from is None or self.effective_from <= on) and (
            self.effective_until is None or on <= self.effective_until
        )


@dataclass(frozen=True)
class ResolvedModel:
    """등록부가 모델 이름을 해석한 결과. 호출을 시작하기 전에 확정한다.

    provider를 응답에서 읽지 않고 미리 확정하는 이유: provider 오류는 결과가 만들어지기
    전에 Sentry로 보고되고(story_llm의 실패 경로), 선택지는 재호출이 다 실패하면 폴백으로
    답해 성공 결과가 아예 없다. 실패·폴백·스트림 오류 경로에서도 값이 살아 있어야 한다.
    """

    model: str  # 실제 호출에 쓸 모델 이름
    provider: str  # 로깅·Sentry 태그용 공급자
    adapter: str  # 어느 SDK 어댑터로 보낼지(ADAPTER_* 중 하나)
    # 이 모델을 추론(thinking) 모드로 부를지. **뜻만 적고 회사 문법은 어댑터가 만든다** —
    # 같은 "추론 끄기"를 DeepSeek은 extra_body 안에, GPT·Anthropic은 요청 최상위 인자로 넣는다.
    # 회사 문법을 등록부에 담으면 공급자가 늘 때마다 등록부를 고쳐야 한다(사용자 결정).
    # 기본값을 두지 않는다 — 새 모델을 올릴 때 추론 모드를 반드시 정하게 한다.
    use_thinking: bool
    # 모델이 temperature를 받는지. 안 받는 모델에 보내면 400으로 거부되므로(예: Anthropic
    # Sonnet 5) 어댑터가 그 인자를 빼고 보낸다 — 값을 몰래 바꾸지 않고 뺀다.
    supports_temperature: bool = True
    # 기간이 겹치지 않는 가격표. 빈 기본값은 테스트용 가짜 모델을 간단히 만들기 위한 것이고,
    # 실제 등록부 모델은 tests/unit/test_llm_registry.py에서 한 개 이상인지 강제한다.
    pricing: tuple[ModelPricing, ...] = ()
    # 공급자 공식 문서의 모델 한도. None 기본값은 테스트용 가짜 모델을 위한 것이고, 실제 등록부는
    # 테스트에서 양의 정수와 max_output <= context 관계를 강제한다.
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    # 실제 요청에 보낼 추론 강도. None은 "공급자 기본값"이 아니라 "이 모델 설정에서는 effort
    # 인자를 쓰지 않는다"는 뜻이다. 허용값 목록과 함께 적어 오타를 기동 검사에서 막는다.
    reasoning_effort: str | None = None
    supported_reasoning_efforts: frozenset[str] = frozenset()
    structured_output_modes: frozenset[str] = frozenset()
    # 기능 정보도 가격처럼 언제 어느 공식 문서로 확인했는지 남긴다. 고정 스냅샷이 따로 없으면
    # snapshot_model은 None이다. Claude 4.6+의 dateless ID는 그 자체가 고정 스냅샷이다.
    capabilities_verified_on: date | None = None
    capabilities_source_urls: tuple[str, ...] = ()
    snapshot_model: str | None = None

    def pricing_on(self, on: date | None = None) -> ModelPricing:
        """지정 날짜(기본값 오늘)에 적용되는 가격표를 반환한다."""
        target = on or date.today()
        matches = [price for price in self.pricing if price.applies_on(target)]
        if len(matches) != 1:
            raise ValueError(
                f"모델 '{self.model}'의 {target.isoformat()} 가격표가 정확히 하나가 아닙니다: "
                f"{len(matches)}개"
            )
        return matches[0]


@dataclass(frozen=True)
class LlmRequest:
    """통로에 넣는 단발 호출 요청.

    messages를 그대로 받는다(system 포함). 호출부마다 system의 개수·위치가 달라
    (스토리라인·컴파일·선택지·판정은 앞 1개, 채팅 본문은 앞 1 + 뒤 2) `system`을 따로 받는
    형태로는 채팅을 표현할 수 없다. 회사 형식으로 옮기는 일은 어댑터가 맡는다.
    """

    model: str
    messages: list[Message]
    max_tokens: int | None = None
    # 이 호출 하나의 제한 시간(초). **None은 무제한이 아니라 "SDK 기본값"이다** — OpenAI SDK
    # 기본은 읽기 600초(10분)다. 지금 60초(선택지·판정)·90초(채팅·스토리)에 끊기는 호출을
    # 통로로 옮길 때 이 값을 반드시 같이 적어야 한다. 비워두면 상한이 조용히 10분으로 늘어난다.
    timeout: float | None = None
    temperature: float | None = None
    json_mode: bool = False  # JSON 객체 응답을 요구한다(OpenAI 계열은 response_format으로 강제)


@dataclass(frozen=True)
class TokenUsage:
    """토큰 사용량.

    `input_tokens`는 **전체 입력**이다. 공급자가 이 숫자를 어떤 모양으로 주는지는 어댑터가 안다.
    어댑터가 회사(SDK)당 하나이므로 계산 규칙도 회사당 하나다 — 등록부에 모델별로 적지 않는다.

    - 합계로 주는 공급자(DeepSeek·GPT) — 받은 값을 그대로 쓴다. 캐시 적중분이 이미 합계에
      포함돼 있어, 여기서 또 더하면 두 번 세어 백엔드 적재값이 부풀려진다.
    - 캐시분을 따로 떼어 주는 공급자(Anthropic) — 일반 + 캐시 생성 + 캐시 읽기를 합산해 채운다.
      합산하지 않으면 캐시가 잘 걸릴수록 실제보다 작은 값이 적재된다.

    아래 두 필드는 내역(진단용)이다. 값이 아예 없으면 0이 아니라 None으로 남긴다 —
    백엔드 계약이 "누락 시 null"이다(응답 meta의 토큰 필드가 `int | None`).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass(frozen=True)
class LlmResult:
    """단발 호출의 결과. model은 응답이 돌려준 실제 모델명이다.

    `text`는 **빈 문자열일 수 있다.** 응답은 왔는데 본문이 비었거나 응답 껍데기가 깨진 경우
    (빈 choices·message 없음)에도 어댑터는 예외를 던지지 않고 `text=""`로 돌려준다 —
    쓸 수 있는 응답인지 판정하는 일은 호출부가 한다. 어댑터가 이걸 전송 오류로 던지면
    스토리라인의 invalid 재호출(KNK-312, 최대 2회)이 사라져 첫 실패가 곧바로 502가 된다.

    `model`은 공급자 서버가 응답 봉투에 붙여준 라벨이다(LLM이 생성한 글이 아니다). 스트리밍
    조각처럼 이 칸이 비어 오는 경우가 있어, 어댑터는 **요청에 쓴 모델 이름으로 채운다** —
    지금 호출부 네 곳이 각자 하고 있는 `response.model or 요청모델` 폴백을 한 자리로 모은 것이다.
    빈 값을 올리면 응답 meta 조립에서 터지고, 채팅은 이미 토큰을 흘려보낸 뒤라 오류 이벤트도
    못 내고 끊긴다.
    """

    text: str
    model: str
    provider: str
    usage: TokenUsage
    finish_reason: str | None = None  # 잘림 감지용(max_tokens 초과)


@dataclass(frozen=True)
class TextDelta:
    """스트림 도중의 부분 문장."""

    text: str


@dataclass(frozen=True)
class StreamCompleted:
    """스트림이 정상적으로 끝났음 + 사용 메타. 오류로 끝나면 이 이벤트는 나오지 않는다."""

    model: str
    provider: str
    usage: TokenUsage
    finish_reason: str | None = None


StreamEvent: TypeAlias = TextDelta | StreamCompleted


class LlmAdapter(Protocol):
    """어댑터가 갖춰야 할 함수 셋. 어댑터는 클래스가 아니라 **모듈**이 이 모양을 만족한다.

    이 레포에는 타입 검사기(mypy)가 설정돼 있지 않아 **실행 중에 강제되지는 않는다** —
    편집기와 이 파일이 계약을 알려주는 역할이다. 그래도 적어두는 이유는, 다음 어댑터
    (Anthropic — KNK-675)를 만들 때 함수 이름을 다르게 지으면 통로가 그 모듈을 부르는
    순간에야 AttributeError로 드러나기 때문이다. 여기가 "무엇을 만들면 되는지"의 정본이다.
    """

    # 이 어댑터가 조각 흘리기를 할 수 있는지. **기본값을 두지 않는다** — 새 어댑터가 이 값을
    # 빠뜨리면 기동 검사가 AttributeError로 즉시 드러낸다. 기본값을 True로 두면 못 하는
    # 어댑터가 채팅에 꽂혀도 기동이 통과하고, 첫 사용자 요청에서야 터진다(KNK-676 리뷰 P2).
    #
    # **"어느 용도가 스트리밍인지"는 어댑터가 모른다.** 그 판정은 등록부가 한다
    # (`registry.STREAMING_ENVS`) — 어댑터는 자기가 할 수 있는지만 밝힌다.
    SUPPORTS_STREAMING: bool

    def check_supported(self, resolved: ResolvedModel) -> None:
        """이 모델의 설정을 요청 인자로 표현할 수 있는지 확인한다. 못 하면 LlmConfigError."""
        ...

    async def complete(self, req: LlmRequest, resolved: ResolvedModel) -> LlmResult:
        """단발 호출."""
        ...

    def stream(self, req: LlmRequest, resolved: ResolvedModel) -> AsyncIterator[StreamEvent]:
        """스트리밍 호출."""
        ...


class LlmError(Exception):
    """공급자 전송 오류의 공통 상위 — 어댑터가 회사별 SDK 예외를 이 계열로 번역해 던진다.

    타임아웃·429·요청거부·연결실패처럼 **응답을 받지 못한** 실패만 이 계열이다.
    응답은 왔는데 내용물이 못 쓸 때(깨진 JSON·계약 위반)는 호출부의 내용물 오류로 남긴다 —
    스토리라인 재호출(KNK-312)이 내용물 오류에서만 돌기 때문에, 둘을 섞으면 재호출이 오작동한다.

    provider·model을 예외에 실어 보낸다 — 실패 경로에는 결과 객체가 없어서 Sentry 태그가
    가리킬 값이 예외밖에 없다.
    """

    def __init__(self, message: str, *, provider: str, model: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


# 어댑터가 SDK 예외를 아래 4종으로 접을 때 **검사 순서에 주의한다.** SDK에서 타임아웃은
# 연결 오류의 하위 클래스다(openai `APITimeoutError` ⊂ `APIConnectionError`). 연결 오류를
# 먼저 검사하면 모든 타임아웃이 LlmUnavailable로 분류돼 error_code와 사용자 문구까지 바뀐다.
# 타임아웃을 먼저 본다.


class LlmTimeout(LlmError):
    """응답 시간 초과 → AN-4-7 provider_timeout."""


class LlmRateLimited(LlmError):
    """요청량 제한(429) → AN-4-7 provider_rate_limited."""


class LlmBadRequest(LlmError):
    """요청 내용이 잘못돼 거부됨(400) → AN-4-7 provider_bad_request.

    **4xx 전부가 아니다.** 인증(401)·권한(403)·없는 경로(404)·처리 불가(422)는 4xx지만
    아래 LlmUnavailable로 접는다 — 지금 서버가 그렇게 분류하고(`sentry.classify_error_code`는
    `BadRequestError`만 bad_request로 본다) 스펙 §5-5도 "인증·5xx 등 그 외"를 unavailable로 둔다.
    이 경계를 넓히면 같은 실패의 error_code와 502 문구가 조용히 바뀐다.
    """


class LlmUnavailable(LlmError):
    """연결 실패·인증·권한·404·422·5xx, 그리고 위 셋에 안 걸리는 provider 오류 → provider_unavailable.

    어댑터가 이 예외로 접는 대상은 둘이다 — **SDK 예외 중 위 셋에 안 걸리는 것**과, **SDK 밖으로
    새는 전송·파싱 오류**(스트림 도중의 연결 끊김·깨진 SSE 줄). 번역되지 않은 전송 오류가
    호출부까지 올라가면 실패를 흡수해야 하는 경로(선택지 폴백·판정 null)가 흡수에 실패해
    턴이 500으로 깨진다.

    반대로 **우리 코드의 결함(오타·형 실수)은 접지 않는다.** 공급자 장애로 위장되면 관측이
    거짓이 되고 원인 추적이 헛돈다(STYLEGUIDE §4 — 모든 예외를 삼키지 않는다).
    """


class LlmConfigError(Exception):
    """설정 오류 — 미등록 모델, 선택된 모델의 키 부재 등. 서버 기동에서 드러나야 하는 문제다.

    LlmError를 상속하지 않는다. 호출부의 `except LlmError`가 설정 오류까지 삼켜 502로 바꾸면
    "모델 이름을 잘못 적었다"는 사실이 provider 장애로 위장된다.
    """
