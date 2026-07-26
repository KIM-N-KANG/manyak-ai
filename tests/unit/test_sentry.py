import json

import httpx
import pytest

from src.core import sentry
from src.core.sentry import capture_ai_exception, classify_error_code, init_sentry
from src.services.llm.base import (
    LlmBadRequest,
    LlmError,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
)


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/v1")


# ── error_code 분류 (AN-4-7) ────────────────────────────────────────────────
def test_classify_timeout() -> None:
    from openai import APITimeoutError

    assert classify_error_code(APITimeoutError(request=_req())) == "provider_timeout"


def test_classify_rate_limited() -> None:
    from openai import RateLimitError

    resp = httpx.Response(429, request=_req())
    assert (
        classify_error_code(RateLimitError("rate", response=resp, body=None))
        == "provider_rate_limited"
    )


def test_classify_bad_request() -> None:
    from openai import BadRequestError

    resp = httpx.Response(400, request=_req())
    assert (
        classify_error_code(BadRequestError("bad", response=resp, body=None))
        == "provider_bad_request"
    )


def test_classify_connection_unavailable() -> None:
    from openai import APIConnectionError

    assert classify_error_code(APIConnectionError(request=_req())) == "provider_unavailable"


# ── 공급자 중립 예외 분류 (KNK-670) ──────────────────────────────────────────
# 이 분기가 없으면 통로 이관 후 모든 전송 실패가 unexpected_error로 떨어진다.
@pytest.mark.parametrize(
    ("exc_class", "expected"),
    [
        (LlmTimeout, "provider_timeout"),
        (LlmRateLimited, "provider_rate_limited"),
        (LlmBadRequest, "provider_bad_request"),
        (LlmUnavailable, "provider_unavailable"),
    ],
)
def test_classify_neutral_llm_errors(exc_class: type[LlmError], expected: str) -> None:
    exc = exc_class("실패", provider="deepseek", model="deepseek-v4-flash")

    assert classify_error_code(exc) == expected


def test_classify_unknown_neutral_error_falls_back_to_unavailable() -> None:
    """중립 예외가 늘어나도 unexpected_error로 새지 않는다 — LlmError 계열은 전부 잡힌다."""

    class _FutureLlmError(LlmError):
        pass

    exc = _FutureLlmError("실패", provider="deepseek", model="deepseek-v4-flash")

    assert classify_error_code(exc) == "provider_unavailable"


def test_classify_json_decode_invalid_response() -> None:
    try:
        json.loads("{not json")
    except json.JSONDecodeError as e:
        assert classify_error_code(e) == "invalid_ai_response"


def test_classify_unexpected() -> None:
    assert classify_error_code(ValueError("기타")) == "unexpected_error"


# ── init (DSN 유무) ─────────────────────────────────────────────────────────
def test_init_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict = {}
    monkeypatch.setattr(sentry.settings, "sentry_dsn", "")
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: called.update(kw))
    init_sentry()
    assert called == {}  # DSN 없으면 init 호출 안 함(no-op)


def test_init_with_dsn_passes_options(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict = {}
    monkeypatch.setattr(sentry.settings, "sentry_dsn", "https://k@o.ingest.sentry.io/1")
    monkeypatch.setattr(sentry.settings, "sentry_environment", "test-env")
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: called.update(kw))
    init_sentry()
    assert called["dsn"] == "https://k@o.ingest.sentry.io/1"
    assert called["environment"] == "test-env"
    assert called["send_default_pii"] is False  # AN-4-10


# ── before_send PII 차단 (AN-4-10) ──────────────────────────────────────────
def test_before_send_strips_request() -> None:
    event = {"request": {"data": "프롬프트 원문"}, "message": "LLM 오류"}
    assert sentry._before_send(event, {}) == {"message": "LLM 오류"}


# ── capture: tag·context 구성 ───────────────────────────────────────────────
class _FakeScope:
    def __init__(self) -> None:
        self.tags: dict = {}
        self.contexts: dict = {}

    def set_tag(self, k: str, v: object) -> None:
        self.tags[k] = v

    def set_context(self, k: str, v: object) -> None:
        self.contexts[k] = v

    def __enter__(self) -> "_FakeScope":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def test_capture_sets_tags_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _FakeScope()
    captured: dict = {}
    monkeypatch.setattr(sentry.sentry_sdk, "new_scope", lambda: scope)
    monkeypatch.setattr(
        sentry.sentry_sdk, "capture_exception", lambda e: captured.setdefault("exc", e)
    )

    err = ValueError("boom")
    capture_ai_exception(
        err,
        feature="chat_response",
        error_code="unexpected_error",
        model="deepseek-v4-flash",
        prompt_versions={"SAFETY": 1, "CORE": 2},
        retry_count=1,
    )

    assert scope.tags["feature"] == "chat_response"
    assert scope.tags["error_code"] == "unexpected_error"
    assert scope.tags["provider"] == "deepseek"
    assert scope.tags["model"] == "deepseek-v4-flash"
    # prompt_versions는 dict 그대로 context에 싣는다(KNK-246 계약과 일치)
    assert scope.contexts["ai"]["prompt_versions"] == {"SAFETY": 1, "CORE": 2}
    assert scope.contexts["ai"]["retry_count"] == 1
    assert captured["exc"] is err


def test_capture_classifies_when_error_code_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _FakeScope()
    monkeypatch.setattr(sentry.sentry_sdk, "new_scope", lambda: scope)
    monkeypatch.setattr(sentry.sentry_sdk, "capture_exception", lambda e: None)

    from openai import APITimeoutError

    capture_ai_exception(APITimeoutError(request=_req()), feature="story_completion")
    assert scope.tags["error_code"] == "provider_timeout"  # 미지정 시 타입으로 분류
