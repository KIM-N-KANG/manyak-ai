import io
import json

import httpx
import pytest
import sentry_sdk

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


# ── 시크릿·원문이 스택 프레임으로 새지 않는지 (KNK-671 리뷰) ─────────────────
# 두 층으로 나눈다. "운영 코드가 그 옵션을 넘기는가"는 아래 test_init_with_dsn_passes_options가
# init을 가로채 확인하고(전역 오염 없음), "그 옵션이 정말 값을 막는가"는 실제 초기화가 필요해
# 여기서 확인한다.


class _CollectingTransport(sentry_sdk.transport.Transport):
    """전송 대신 직렬화된 봉투(envelope) 원문을 모은다.

    함수 하나를 `transport=`로 넘기는 옛 방식은 폐기 예정 경고가 뜬다. 그리고 모은 것을
    `str()`로 훑으면 실제 전송될 바이트가 아니라 객체 표기를 보게 돼 검증이 헛돈다 —
    직렬화까지 해서 **나가는 내용 그대로**를 본다.
    """

    def __init__(self, sink: list[str]) -> None:
        super().__init__()
        self._sink = sink

    def capture_envelope(self, envelope: object) -> None:
        buf = io.BytesIO()
        envelope.serialize_into(buf)  # type: ignore[attr-defined]
        self._sink.append(buf.getvalue().decode("utf-8", errors="replace"))


def test_secrets_in_locals_do_not_reach_sentry() -> None:
    """설정만 확인하지 않고 **실제 전송되는 이벤트**에 시크릿이 없는지 본다.

    옵션 이름만 단언하면 "그 옵션이 정말 그 일을 하는가"는 증명되지 않는다.

    통합(integration)은 전부 끄고 켠다 — sentry_sdk.init은 프로세스 전역이라, 켜두면 이
    테스트 이후의 모든 테스트가 HTTP·예외 처리에 후크가 붙은 다른 환경에서 돌게 된다.
    """
    sent: list[str] = []
    api_key = "sk-live-must-not-leak"
    prompt = "사용자가 쓴 프롬프트 원문"
    isolated = {"default_integrations": False, "auto_enabling_integrations": False}

    try:
        sentry_sdk.init(
            dsn="https://pub@example.invalid/1",
            transport=_CollectingTransport(sent),  # 실제 전송 대신 여기로 모은다
            include_local_variables=False,
            before_send=sentry._before_send,
            **isolated,
        )

        def _fails_with_secrets_in_scope() -> None:
            creds = {"api_key": api_key}  # noqa: F841 — 프레임에 남기는 것이 시험 대상
            messages = [{"role": "user", "content": prompt}]  # noqa: F841
            raise RuntimeError("접속 준비 실패")

        try:
            _fails_with_secrets_in_scope()
        except RuntimeError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn="", **isolated)  # 전역 상태 원복(이후 테스트가 실제 전송을 하지 않도록)

    assert sent, "이벤트가 전송되지 않아 검증이 무의미하다"
    payload = "\n".join(sent)
    for secret in (api_key, prompt):
        # 봉투는 ASCII로 이스케이프된 JSON이라 한국어 원문이 \uXXXX 형태로 들어간다.
        # 원문 그대로만 찾으면 프롬프트 검사가 **무슨 일이 있어도 통과**해 버린다(실측 확인).
        assert secret not in payload
        assert json.dumps(secret)[1:-1] not in payload


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
    # 지역변수 수집 끄기(KNK-671). 이 단언이 없으면 운영 코드에서 옵션을 지워도 테스트는
    # 통과한다 — 실제 차단 효과는 test_secrets_in_locals_do_not_reach_sentry가 따로 본다.
    assert called["include_local_variables"] is False


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
