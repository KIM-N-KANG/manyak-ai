"""Sentry 오류 수집 (KNK-262) — AI 호출 실패를 분석 명세(AN-4) 기준으로 보고한다.

DSN이 비어 있으면 init이 no-op이라 로컬·CI는 영향이 없다(분석 명세대로 prod에서만 켠다).
프롬프트·채팅·LLM 생성 원문은 Sentry에 싣지 않는다(AN-4-10) — send_default_pii=False와
before_send로 request 데이터를 떼고, 캡처 인자에도 원문을 넣지 않는다.

manyak-ai가 자체적으로 아는 값(feature·provider·model·error_code tag, prompt_versions·
retry_count·latency_ms context)만 여기서 싣는다. 백엔드가 헤더로 넘긴 요청 상관관계 식별자
(request_id·session_id·anonymous_id_hash)는 RequestContextMiddleware가 요청별 Sentry
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

logger = logging.getLogger(__name__)

# AN-4-3 AI feature 식별자 — server AiCallFeature 값과 동일하게 맞춘다.
FEATURE_STORYLINE_GENERATION = "storyline_generation"
FEATURE_STORY_COMPLETION = "story_completion"
FEATURE_CHAT_RESPONSE = "chat_response"
# 선택지 생성(2번째 호출)의 AI측 오류 그룹용 태그. 백엔드에는 별도 ai_call_log가 아니라
# chat_response meta에 합산돼 적재되므로, 이 값은 AI 서비스 Sentry 그룹핑 전용이다.
FEATURE_CHAT_NEXT_ACTIONS = "chat_next_actions"

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
    """
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
        before_send=_before_send,
    )


def capture_ai_exception(
    exc: BaseException,
    *,
    feature: str,
    error_code: str | None = None,
    model: str | None = None,
    prompt_versions: dict | None = None,
    retry_count: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """AI 호출 실패를 Sentry에 보고한다(AN-4-8). DSN 미설정 시 자동 no-op.

    원문(프롬프트·응답)은 인자로 받지 않는다 — feature·provider·model·error_code(tag)와
    prompt_versions·retry_count·latency_ms(context)만 싣는다. error_code가 없으면
    예외 타입으로 분류한다(classify_error_code).
    """
    if error_code is None:
        error_code = classify_error_code(exc)
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("feature", feature)
        scope.set_tag("provider", settings.llm_provider)
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
