"""Sentry 오류 수집 (KNK-262) — AI 호출 실패를 분석 명세(AN-4) 기준으로 보고한다.

DSN이 비어 있으면 init이 no-op이라 로컬·CI는 영향이 없다(분석 명세대로 prod에서만 켠다).
프롬프트·채팅·LLM 생성 원문은 Sentry에 싣지 않는다(AN-4-10) — send_default_pii=False와
before_send로 request 데이터를 떼고, 캡처 인자에도 원문을 넣지 않는다.

manyak-ai가 자체적으로 아는 값(feature·provider·model·error_code tag, prompt_versions·
retry_count·latency_ms context)만 여기서 싣는다. 백엔드가 헤더로 넘긴 요청 상관관계 식별자
(request_id·session_id·device_id_hash)는 RequestContextMiddleware가 요청별 Sentry
isolation scope에 직접 부착한다(KNK-266) — 미처리 500 캡처까지 커버되도록.

prompt_versions는 단일 문자열이 아니라 dict로 싣는다 — chat은 6레이어 다중 키라
단일 스칼라로 담을 수 없어 백엔드도 JSONB로 적재한다(KNK-246). 그 계약과 표현을 맞춘다.
"""

import json
import logging

import sentry_sdk
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    OpenAIError,
    RateLimitError,
)

from src.core.config import settings

# 공급자 중립 예외를 AN-4-7 코드로 접기 위해 가져온다(KNK-670). base는 src 안의 다른 것을
# 임포트하지 않아 순환이 생기지 않는다 — 여기가 이미 openai 예외 타입을 아는 자리이기도 하다.
from src.services.llm.base import (
    LlmBadRequest,
    LlmError,
    LlmRateLimited,
    LlmTimeout,
)
# 이미지 생성 통로의 중립 예외도 같은 코드로 접는다(PR #92 리뷰). image.base 역시 src 안의
# 다른 것을 임포트하지 않는다.
from src.services.image.base import (
    ImageBadRequest,
    ImageGenerationError,
    ImageRateLimited,
    ImageTimeout,
)

logger = logging.getLogger(__name__)

# AN-4-3 AI feature 식별자 — server AiCallFeature 값과 동일하게 맞춘다.
FEATURE_STORYLINE_GENERATION = "storyline_generation"
FEATURE_STORY_COMPLETION = "story_completion"
FEATURE_CHAT_RESPONSE = "chat_response"
# 선택지 생성(/chat/choices 전용 엔드포인트 — KNK-625)의 AI측 오류 그룹용 태그.
# 분리 후 백엔드도 같은 값의 ai_call_logs 별도 행(choice_generation)으로 적재한다
# (예약값 활성화). 값은 server AiCallFeature.CHOICE_GENERATION(KNK-365)과 맞춘다.
FEATURE_CHOICE_GENERATION = "choice_generation"
# 컴파일 인물 이미지 생성(KNK-939)의 AI측 오류 그룹용 태그. 이미지 실패는 컴파일을 깨지 않고
# 해당 인물만 비우므로 로그로만 남으면 아무도 모른다 — 시간 초과·429·거부를 여기로 모은다.
# 백엔드 AiCallFeature에는 대응값이 없다(AI 서버 전용 태그).
FEATURE_CHARACTER_IMAGE = "character_image_generation"
# 컴파일 스토리 썸네일(표지) 생성(KNK-1047)의 AI측 오류 그룹용 태그. 인물 이미지와 같은
# 통로를 쓰지만 실패 원인(세로 크기 거부 등)을 따로 보려고 태그를 나눈다. AI 서버 전용 태그.
FEATURE_THUMBNAIL_IMAGE = "thumbnail_image_generation"

# AN-4-7 실패 코드.
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
ERROR_PROVIDER_RATE_LIMITED = "provider_rate_limited"
ERROR_PROVIDER_BAD_REQUEST = "provider_bad_request"
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERROR_INVALID_AI_RESPONSE = "invalid_ai_response"
ERROR_SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
ERROR_UNEXPECTED = "unexpected_error"


def classify_error_code(exc: BaseException) -> str:
    """예외를 AN-4-7 실패 코드로 분류한다.

    빈/비객체 응답(invalid_ai_response)과 schema 검증 실패는 호출부가 error_code를
    명시 전달하므로 여기서는 provider 오류·JSON 파싱 실패만 다룬다.

    공급자 중립 예외(LlmError 계열)를 먼저 본다(KNK-670). 이 분기가 없으면 통로 이관 후
    모든 전송 실패가 unexpected_error로 떨어져 AN-4-7 관측이 통째로 무너진다. OpenAI SDK
    예외 분기는 이관이 끝날 때까지(KNK-672·673) 함께 남는다.
    """
    if isinstance(exc, LlmTimeout):
        return ERROR_PROVIDER_TIMEOUT
    if isinstance(exc, LlmRateLimited):
        return ERROR_PROVIDER_RATE_LIMITED
    if isinstance(exc, LlmBadRequest):
        return ERROR_PROVIDER_BAD_REQUEST
    if isinstance(exc, LlmError):
        # LlmUnavailable과, 혹시 늘어날 다른 중립 예외까지 일시 장애로 묶는다(코드를 적게 유지).
        return ERROR_PROVIDER_UNAVAILABLE
    # 이미지 통로 중립 예외 — 텍스트 LLM과 같은 분류 규칙을 적용한다.
    if isinstance(exc, ImageTimeout):
        return ERROR_PROVIDER_TIMEOUT
    if isinstance(exc, ImageRateLimited):
        return ERROR_PROVIDER_RATE_LIMITED
    if isinstance(exc, ImageBadRequest):
        return ERROR_PROVIDER_BAD_REQUEST
    if isinstance(exc, ImageGenerationError):
        return ERROR_PROVIDER_UNAVAILABLE
    if isinstance(exc, APITimeoutError):
        return ERROR_PROVIDER_TIMEOUT
    if isinstance(exc, RateLimitError):
        return ERROR_PROVIDER_RATE_LIMITED
    if isinstance(exc, BadRequestError):
        return ERROR_PROVIDER_BAD_REQUEST
    if isinstance(exc, APIConnectionError):
        return ERROR_PROVIDER_UNAVAILABLE
    if isinstance(exc, OpenAIError):
        # 인증·5xx 등 그 외 provider 오류는 일시 장애로 묶는다(MVP는 코드를 적게 유지).
        return ERROR_PROVIDER_UNAVAILABLE
    if isinstance(exc, json.JSONDecodeError):
        return ERROR_INVALID_AI_RESPONSE
    return ERROR_UNEXPECTED


def _before_send(event: dict, hint: dict) -> dict:
    """AN-4-10: 프롬프트·채팅·생성 원문이 새지 않게 request 데이터를 떼고 보낸다.

    send_default_pii=False와 함께 이중 안전장치다(우리는 캡처 인자에도 원문을 넣지 않는다).
    요청 상관관계 식별자는 RequestContextMiddleware가 isolation scope에 부착한다(KNK-266).
    """
    event.pop("request", None)
    return event


def init_sentry() -> None:
    """앱 시작 시 Sentry를 초기화한다. DSN이 비면 no-op(로컬·CI는 끈다)."""
    if not settings.sentry_dsn:
        logger.info("SENTRY_DSN 미설정 — Sentry 비활성(no-op)")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,  # AN-4-10 — 식별자·원문 PII 미전송
        # 스택 프레임의 지역변수를 싣지 않는다(기본은 실음). 실으면 예외가 난 함수의 변수가
        # 그대로 전송되는데, 우리 코드에는 API 키(LLM 클라이언트 생성부)와 프롬프트·채팅 원문
        # (LLM 호출부)이 지역변수로 들어 있다 — AN-4-10 원문 비수집과 정면으로 충돌한다.
        # 대가로 원인 추적 시 변수 값을 못 보지만, 시크릿·원문 유출보다 낫다(KNK-671 리뷰).
        include_local_variables=False,
        before_send=_before_send,
    )


def capture_ai_exception(
    exc: BaseException,
    *,
    feature: str,
    provider: str,
    error_code: str | None = None,
    model: str | None = None,
    prompt_versions: dict | None = None,
    retry_count: int | None = None,
    latency_ms: int | None = None,
    level: str | None = None,
) -> None:
    """AI 호출 실패를 Sentry에 보고한다(AN-4-8). DSN 미설정 시 자동 no-op.

    level은 요청 자체는 성공으로 내보내면서 기록만 남기는 완화 경로(KNK-1102,
    스토리라인 이름 등장 미충족)가 "warning"으로 지정한다. 미지정이면 error.

    원문(프롬프트·응답)은 인자로 받지 않는다 — feature·provider·model·error_code(tag)와
    prompt_versions·retry_count·latency_ms(context)만 싣는다. error_code가 없으면
    예외 타입으로 분류한다(classify_error_code).

    provider는 **기본값 없는 필수 인자**다(KNK-674). 예전에는 전역 설정값 하나를 여기서
    직접 읽었는데, 그러면 공급자를 둘 이상 쓰는 순간 모든 실패 태그가 한 값으로 눌린다.
    기본값을 두면 새 호출부가 조용히 그 값을 물려받으므로, 부르는 쪽이 반드시 적게 한다 —
    호출부는 `llm.provider_of(model)`이나 중립 예외의 `.provider`로 얻는다.
    """
    if error_code is None:
        error_code = classify_error_code(exc)
    with sentry_sdk.new_scope() as scope:
        if level is not None:
            scope.set_level(level)
        scope.set_tag("feature", feature)
        scope.set_tag("provider", provider)
        scope.set_tag("error_code", error_code)
        if model:
            scope.set_tag("model", model)
        context: dict = {}
        if prompt_versions is not None:
            context["prompt_versions"] = prompt_versions
        if retry_count is not None:
            context["retry_count"] = retry_count
        if latency_ms is not None:
            context["latency_ms"] = latency_ms
        if context:
            scope.set_context("ai", context)
        sentry_sdk.capture_exception(exc)
