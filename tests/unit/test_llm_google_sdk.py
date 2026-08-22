"""Google SDK 어댑터 테스트 (KNK-957).

라이브 호출은 없다. SDK 호출을 monkeypatch로 대체하고 설정 조립·응답 해석·에러 변환을 검증한다.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import errors, types

from src.services.llm import google_sdk
from src.services.llm.base import (
    ADAPTER_GOOGLE_SDK,
    PROVIDER_GOOGLE,
    STRUCTURED_OUTPUT_JSON_OBJECT,
    LlmBadRequest,
    LlmConfigError,
    LlmRateLimited,
    LlmRequest,
    LlmTimeout,
    LlmUnavailable,
    ResolvedModel,
    StreamCompleted,
    TextDelta,
    TokenUsage,
)
from src.services.llm.registry import ProviderCredentials

# ── 공통 픽스처 ────────────────────────────────────────────────────────────

_RESOLVED = ResolvedModel(
    model="gemini-test",
    provider=PROVIDER_GOOGLE,
    adapter=ADAPTER_GOOGLE_SDK,
    use_thinking=True,
    reasoning_effort="medium",
    supports_temperature=True,
    structured_output_modes=[STRUCTURED_OUTPUT_JSON_OBJECT],
)

_RESOLVED_NO_THINKING = ResolvedModel(
    model="gemini-test",
    provider=PROVIDER_GOOGLE,
    adapter=ADAPTER_GOOGLE_SDK,
    use_thinking=False,
    supports_temperature=True,
)

_RESOLVED_NO_TEMP = ResolvedModel(
    model="gemini-test",
    provider=PROVIDER_GOOGLE,
    adapter=ADAPTER_GOOGLE_SDK,
    use_thinking=False,
    supports_temperature=False,
)


def _req(**overrides) -> LlmRequest:
    defaults = dict(
        model="gemini-test",
        messages=[
            {"role": "system", "content": "시스템 지시"},
            {"role": "user", "content": "안녕"},
        ],
    )
    defaults.update(overrides)
    return LlmRequest(**defaults)


def _fake_response(text="응답", model_version="gemini-test-v1",
                    prompt_tokens=100, candidate_tokens=200, thoughts_tokens=50,
                    finish_reason_name="STOP"):
    """SDK 응답과 같은 모양의 가짜 객체."""
    reason = SimpleNamespace(name=finish_reason_name) if finish_reason_name else None
    return SimpleNamespace(
        text=text,
        model_version=model_version,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidate_tokens,
            thoughts_token_count=thoughts_tokens,
            cached_content_token_count=None,
        ),
        candidates=[SimpleNamespace(finish_reason=reason)],
    )


@pytest.fixture()
def _patch_client(monkeypatch):
    """Google 클라이언트 생성을 우회한다."""
    monkeypatch.setattr(
        google_sdk, "_client",
        lambda provider: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=AsyncMock(return_value=_fake_response()),
            generate_content_stream=AsyncMock(),
        ))),
    )


@pytest.fixture(autouse=True)
def _isolated_client_cache():
    """테스트 간 클라이언트 캐시 격리."""
    saved = google_sdk._clients.copy()
    google_sdk._clients.clear()
    yield
    google_sdk._clients.clear()
    google_sdk._clients.update(saved)


# ── _build_config ──────────────────────────────────────────────────────────

def test_build_config_separates_system_instruction() -> None:
    req = _req()
    config = google_sdk._build_config(req, _RESOLVED)
    assert config.system_instruction == "시스템 지시"


def test_build_config_joins_multiple_system_messages() -> None:
    req = _req(messages=[
        {"role": "system", "content": "첫째"},
        {"role": "user", "content": "질문"},
        {"role": "system", "content": "둘째"},
    ])
    config = google_sdk._build_config(req, _RESOLVED)
    assert config.system_instruction == "첫째\n\n둘째"


def test_build_config_sets_temperature() -> None:
    req = _req(temperature=0.5)
    config = google_sdk._build_config(req, _RESOLVED)
    assert config.temperature == 0.5


def test_build_config_drops_temperature_when_unsupported() -> None:
    req = _req(temperature=0.5)
    config = google_sdk._build_config(req, _RESOLVED_NO_TEMP)
    assert config.temperature is None


def test_build_config_sets_max_output_tokens() -> None:
    req = _req(max_tokens=1000)
    config = google_sdk._build_config(req, _RESOLVED)
    assert config.max_output_tokens == 1000


def test_build_config_rejects_max_tokens_over_limit() -> None:
    resolved = ResolvedModel(
        model="gemini-test", provider=PROVIDER_GOOGLE, adapter=ADAPTER_GOOGLE_SDK,
        use_thinking=False, max_output_tokens=500,
    )
    with pytest.raises(LlmConfigError, match="최대 출력"):
        google_sdk._build_config(_req(max_tokens=1000), resolved)


def test_build_config_sets_json_mode() -> None:
    req = _req(json_mode=True)
    config = google_sdk._build_config(req, _RESOLVED)
    assert config.response_mime_type == "application/json"


def test_build_config_sets_thinking_config() -> None:
    req = _req()
    config = google_sdk._build_config(req, _RESOLVED)
    # SDK가 enum으로 반환할 수 있으므로 문자열 변환 후 비교
    level = config.thinking_config.thinking_level
    assert str(level).lower().replace("thinkinglevel.", "") == "medium"


def test_build_config_no_thinking_when_disabled() -> None:
    req = _req()
    config = google_sdk._build_config(req, _RESOLVED_NO_THINKING)
    assert config.thinking_config is None


def test_build_config_rejects_unsupported_reasoning_effort() -> None:
    resolved = ResolvedModel(
        model="gemini-test", provider=PROVIDER_GOOGLE, adapter=ADAPTER_GOOGLE_SDK,
        use_thinking=True, reasoning_effort="ultra",
        supported_reasoning_efforts=["low", "medium", "high"],
    )
    with pytest.raises(LlmConfigError, match="추론 강도"):
        google_sdk._build_config(_req(), resolved)


def test_build_config_sets_timeout_as_integer_ms() -> None:
    req = _req(timeout=30.5)
    config = google_sdk._build_config(req, _RESOLVED)
    assert config.http_options.timeout == 30500


# ── _build_contents ────────────────────────────────────────────────────────

def test_build_contents_excludes_system() -> None:
    req = _req()
    contents = google_sdk._build_contents(req)
    roles = [c.role for c in contents]
    assert "system" not in roles
    assert roles == ["user"]


def test_build_contents_maps_assistant_to_model() -> None:
    req = _req(messages=[
        {"role": "user", "content": "질문"},
        {"role": "assistant", "content": "답변"},
        {"role": "user", "content": "후속"},
    ])
    contents = google_sdk._build_contents(req)
    assert [c.role for c in contents] == ["user", "model", "user"]


# ── _usage_of ──────────────────────────────────────────────────────────────

def test_usage_sums_candidates_and_thoughts() -> None:
    resp = _fake_response(candidate_tokens=200, thoughts_tokens=50)
    usage = google_sdk._usage_of(resp)
    assert usage.output_tokens == 250  # 200 + 50


def test_usage_candidates_only_when_no_thoughts() -> None:
    resp = _fake_response(candidate_tokens=200, thoughts_tokens=None)
    usage = google_sdk._usage_of(resp)
    assert usage.output_tokens == 200


def test_usage_thoughts_only_when_no_candidates() -> None:
    resp = _fake_response()
    resp.usage_metadata.candidates_token_count = None
    resp.usage_metadata.thoughts_token_count = 50
    usage = google_sdk._usage_of(resp)
    assert usage.output_tokens == 50


def test_usage_none_when_both_missing() -> None:
    resp = _fake_response()
    resp.usage_metadata.candidates_token_count = None
    resp.usage_metadata.thoughts_token_count = None
    usage = google_sdk._usage_of(resp)
    assert usage.output_tokens is None


def test_usage_input_tokens() -> None:
    resp = _fake_response(prompt_tokens=100)
    usage = google_sdk._usage_of(resp)
    assert usage.input_tokens == 100


# ── _finish_reason_of ──────────────────────────────────────────────────────

def test_finish_reason_lowercased() -> None:
    resp = _fake_response(finish_reason_name="STOP")
    assert google_sdk._finish_reason_of(resp) == "stop"


def test_finish_reason_none_when_missing() -> None:
    resp = _fake_response()
    resp.candidates = []
    assert google_sdk._finish_reason_of(resp) is None


def test_finish_reason_none_on_broken_response() -> None:
    resp = SimpleNamespace()  # candidates 속성 없음
    assert google_sdk._finish_reason_of(resp) is None


# ── _text_of ───────────────────────────────────────────────────────────────

def test_text_of_extracts_text() -> None:
    resp = _fake_response(text="본문")
    assert google_sdk._text_of(resp) == "본문"


def test_text_of_returns_empty_on_failure() -> None:
    resp = SimpleNamespace()  # text 속성 없음
    assert google_sdk._text_of(resp) == ""


# ── _translate ─────────────────────────────────────────────────────────────

def _api_error(code: int, msg: str = "err") -> errors.APIError:
    exc = errors.APIError.__new__(errors.APIError)
    exc.code = code
    exc.args = (msg,)
    return exc


def test_translate_408_to_timeout() -> None:
    result = google_sdk._translate(_api_error(408), _RESOLVED)
    assert isinstance(result, LlmTimeout)


def test_translate_429_to_rate_limited() -> None:
    result = google_sdk._translate(_api_error(429), _RESOLVED)
    assert isinstance(result, LlmRateLimited)


def test_translate_400_to_bad_request() -> None:
    result = google_sdk._translate(_api_error(400), _RESOLVED)
    assert isinstance(result, LlmBadRequest)


def test_translate_503_to_unavailable() -> None:
    result = google_sdk._translate(_api_error(503), _RESOLVED)
    assert isinstance(result, LlmUnavailable)


def test_translate_httpx_timeout_to_llm_timeout() -> None:
    exc = httpx.ReadTimeout("read timed out")
    result = google_sdk._translate(exc, _RESOLVED)
    assert isinstance(result, LlmTimeout)


def test_translate_httpx_transport_to_unavailable() -> None:
    exc = httpx.ConnectError("connection refused")
    result = google_sdk._translate(exc, _RESOLVED)
    assert isinstance(result, LlmUnavailable)


def test_translate_connection_error_to_unavailable() -> None:
    result = google_sdk._translate(ConnectionError("reset"), _RESOLVED)
    assert isinstance(result, LlmUnavailable)


def test_translate_timeout_error_to_timeout() -> None:
    result = google_sdk._translate(TimeoutError("timed out"), _RESOLVED)
    assert isinstance(result, LlmTimeout)


# ── complete ───────────────────────────────────────────────────────────────

async def test_complete_returns_result(_patch_client) -> None:
    result = await google_sdk.complete(_req(), _RESOLVED)
    assert result.text == "응답"
    assert result.model == "gemini-test-v1"
    assert result.provider == PROVIDER_GOOGLE
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 250  # 200 + 50 thinking
    assert result.finish_reason == "stop"


async def test_complete_api_error_is_translated(monkeypatch) -> None:
    async def boom(*a, **kw):
        raise _api_error(503, "overloaded")
    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=boom,
        ))),
    )
    with pytest.raises(LlmUnavailable, match="overloaded"):
        await google_sdk.complete(_req(), _RESOLVED)


async def test_complete_httpx_timeout_is_translated(monkeypatch) -> None:
    async def boom(*a, **kw):
        raise httpx.ReadTimeout("read timed out")
    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=boom,
        ))),
    )
    with pytest.raises(LlmTimeout):
        await google_sdk.complete(_req(), _RESOLVED)


async def test_complete_connection_error_is_translated(monkeypatch) -> None:
    async def boom(*a, **kw):
        raise ConnectionError("reset")
    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=boom,
        ))),
    )
    with pytest.raises(LlmUnavailable):
        await google_sdk.complete(_req(), _RESOLVED)


# ── stream ─────────────────────────────────────────────────────────────────

async def test_stream_yields_deltas_then_completed(monkeypatch) -> None:
    chunks = [
        SimpleNamespace(
            text="조각1", usage_metadata=None, model_version=None, candidates=[],
        ),
        SimpleNamespace(
            text="조각2",
            usage_metadata=SimpleNamespace(
                prompt_token_count=10, candidates_token_count=20,
                thoughts_token_count=5, cached_content_token_count=None,
            ),
            model_version="gemini-v2",
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        ),
    ]

    async def fake_stream(*a, **kw):
        for c in chunks:
            yield c

    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=AsyncMock(return_value=fake_stream()),
        ))),
    )

    events = []
    async for ev in google_sdk.stream(_req(), _RESOLVED):
        events.append(ev)

    assert isinstance(events[0], TextDelta)
    assert events[0].text == "조각1"
    assert isinstance(events[1], TextDelta)
    assert events[1].text == "조각2"
    assert isinstance(events[2], StreamCompleted)
    assert events[2].model == "gemini-v2"
    assert events[2].finish_reason == "stop"
    assert events[2].usage.output_tokens == 25  # 20 + 5


async def test_stream_api_error_is_translated(monkeypatch) -> None:
    async def fake_stream(*a, **kw):
        yield SimpleNamespace(text="일부", usage_metadata=None, model_version=None, candidates=[])
        raise _api_error(429, "rate limited")

    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=AsyncMock(return_value=fake_stream()),
        ))),
    )

    with pytest.raises(LlmRateLimited):
        async for _ in google_sdk.stream(_req(), _RESOLVED):
            pass


# ── client 캐시 ────────────────────────────────────────────────────────────

def test_fingerprint_is_deterministic() -> None:
    a = google_sdk._fingerprint("test-key-123")
    b = google_sdk._fingerprint("test-key-123")
    assert a == b
    assert len(a) == 12


def test_fingerprint_differs_for_different_keys() -> None:
    a = google_sdk._fingerprint("key-a")
    b = google_sdk._fingerprint("key-b")
    assert a != b


def test_client_rejects_blank_key(monkeypatch) -> None:
    monkeypatch.setattr(
        google_sdk.registry, "credentials",
        lambda p: ProviderCredentials(
            api_key="  ", api_key_env="GEMINI_API_KEY",
            base_url=None, base_url_env="GEMINI_API_URL",
        ),
    )
    with pytest.raises(LlmConfigError, match="비어 있습니다"):
        google_sdk._client(PROVIDER_GOOGLE)


# ── check_supported ────────────────────────────────────────────────────────

def test_check_supported_passes_for_valid_model() -> None:
    google_sdk.check_supported(_RESOLVED)


def test_check_supported_rejects_unsupported_effort() -> None:
    resolved = ResolvedModel(
        model="gemini-test", provider=PROVIDER_GOOGLE, adapter=ADAPTER_GOOGLE_SDK,
        use_thinking=True, reasoning_effort="ultra",
        supported_reasoning_efforts=["low", "medium", "high"],
    )
    with pytest.raises(LlmConfigError):
        google_sdk.check_supported(resolved)
