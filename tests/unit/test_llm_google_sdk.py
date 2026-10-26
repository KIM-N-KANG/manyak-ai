"""Google SDK 어댑터 테스트 (KNK-957).

라이브 호출은 없다. SDK 호출을 monkeypatch로 대체하고 설정 조립·응답 해석·에러 변환을 검증한다.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import errors, types

from src.core import langfuse as lf
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


# ── Langfuse 관측 배선(KNK-1097) ─────────────────────────────────────────────
# 자동 계측(langfuse.openai)이 google-genai를 덮지 않으므로 어댑터가 observe_generation으로
# 손수 기록한다. 실제 Langfuse 전송 없이(무과금) "무엇을 어디에 기록하는가"만 고정한다.


class _Recorder:
    """observe_generation 대역 — 시작 인자와 finish 인자, 블록 예외를 기록한다."""

    def __init__(self) -> None:
        self.started: dict = {}
        self.finished: dict | None = None
        self.exc: BaseException | None = None

    def finish(self, **kwargs) -> None:
        self.finished = {**(self.finished or {}), **kwargs}


def _mock_observation(monkeypatch) -> _Recorder:
    rec = _Recorder()

    @contextmanager
    def fake_observe_generation(name, **kwargs):
        rec.started = {"name": name, **kwargs}
        try:
            yield rec
        except BaseException as exc:
            rec.exc = exc
            raise

    monkeypatch.setattr(google_sdk, "observe_generation", fake_observe_generation)
    return rec


async def test_complete_records_observation_with_usage(monkeypatch, _patch_client) -> None:
    """성공 시: 이름·모델·모델 인자·입력은 시작에, 본문과 usage는 finish에 실린다.
    thinking은 output과 나눠 output_reasoning으로 싣고(관리 단가가 따로 매겨 합치면 이중 과금),
    total을 명시해 Langfuse의 재합산을 막는다."""
    rec = _mock_observation(monkeypatch)
    req = _req(temperature=0.7, max_tokens=1000, json_mode=True)
    await google_sdk.complete(req, _RESOLVED)

    assert rec.started == {
        "name": "Gemini-generation",
        "model": "gemini-test",
        "model_parameters": {
            "temperature": 0.7,
            "max_tokens": 1000,
            "reasoning_effort": "medium",
            "json_mode": True,
        },
        "input_data": req.messages,
    }
    assert rec.finished == {
        "output": "응답",
        "usage_details": {"input": 100, "output": 200, "output_reasoning": 50, "total": 350},
    }
    assert rec.exc is None


def test_usage_details_splits_cached_input() -> None:
    """prompt_token_count는 캐시 적중분을 포함하므로 input과 input_cached_tokens로 나눠 싣는다."""
    response = SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=1000, cached_content_token_count=600,
        candidates_token_count=200, thoughts_token_count=None,
    ))
    assert google_sdk._usage_details(response) == {
        "input": 400, "input_cached_tokens": 600, "output": 200, "total": 1200,
    }


def test_usage_details_none_without_usage() -> None:
    assert google_sdk._usage_details(SimpleNamespace(usage_metadata=None)) is None
    empty = SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=None, cached_content_token_count=None,
        candidates_token_count=None, thoughts_token_count=None,
    ))
    assert google_sdk._usage_details(empty) is None


async def test_complete_failure_marks_observation_with_neutral_exception(monkeypatch) -> None:
    """공급자 실패는 관측 블록을 공급자 중립 예외로 나간다 — 응답이 없으니 finish는 불리지 않는다."""
    rec = _mock_observation(monkeypatch)

    async def boom(*a, **kw):
        raise _api_error(503, "overloaded")

    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=boom,
        ))),
    )
    with pytest.raises(LlmUnavailable) as excinfo:
        await google_sdk.complete(_req(), _RESOLVED)
    assert isinstance(excinfo.value.__cause__, errors.APIError)  # 원래 SDK 예외 체인 유지
    assert isinstance(rec.exc, LlmUnavailable)
    assert rec.finished is None


def test_usage_details_clamps_negative_input() -> None:
    """cached가 prompt보다 큰 어긋난 응답도 음수 input(음수 비용)을 만들지 않는다."""
    response = SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=100, cached_content_token_count=150,
        candidates_token_count=10, thoughts_token_count=None,
    ))
    assert google_sdk._usage_details(response) == {
        "input": 0, "input_cached_tokens": 150, "output": 10, "total": 110,
    }


def test_usage_details_partial_fields() -> None:
    """빠진 필드는 싣지 않는다 — prompt가 없으면 cached도 버리고, thinking만 있으면 그것만 싣는다."""
    response = SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=None, cached_content_token_count=30,
        candidates_token_count=None, thoughts_token_count=40,
    ))
    assert google_sdk._usage_details(response) == {"output_reasoning": 40, "total": 40}


class _RecordingStreamGeneration:
    def __init__(self, started: dict) -> None:
        self.started = started
        self.ends: list[tuple[str, dict]] = []

    def mark_first_chunk(self) -> None:
        return None

    def finish(self, **fields) -> None:
        self.ends.append(("finish", fields))

    def fail(self, exc, **fields) -> None:
        self.ends.append(("fail", {"exc": exc, **fields}))

    def cancel(self, **fields) -> None:
        self.ends.append(("cancel", fields))


def _record_stream_generation(monkeypatch) -> list[_RecordingStreamGeneration]:
    made: list[_RecordingStreamGeneration] = []

    def _start(name, **kwargs):
        generation = _RecordingStreamGeneration({"name": name, **kwargs})
        made.append(generation)
        return generation

    monkeypatch.setattr(google_sdk, "start_stream_generation", _start)
    return made


def _install_stream(monkeypatch, chunks: list, error: BaseException | None = None) -> None:
    async def fake_stream(*a, **kw):
        for c in chunks:
            yield c
        if error is not None:
            raise error

    monkeypatch.setattr(
        google_sdk, "_client",
        lambda p: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=AsyncMock(return_value=fake_stream()),
        ))),
    )


_USAGE_CHUNK = SimpleNamespace(
    text="녕",
    usage_metadata=SimpleNamespace(
        prompt_token_count=100, candidates_token_count=20,
        thoughts_token_count=5, cached_content_token_count=40,
    ),
    model_version=None,
    candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
)


async def test_stream_records_one_finished_generation(monkeypatch) -> None:
    """채팅 본문(스트림)도 Langfuse에 남긴다. 고치기 전에는 Gemini 채팅 턴이 기록에서 통째로 빠졌다.

    현재 컨텍스트 관측(`observe_generation`)은 쓰지 않는다 — yield를 넘나들면 중도 이탈이 ERROR로
    남고 부모가 틀어진다. 스트림 전용 관측을 한 번 열고 한 번 닫는다.
    """
    rec = _mock_observation(monkeypatch)
    made = _record_stream_generation(monkeypatch)
    first = SimpleNamespace(text="안", usage_metadata=None, model_version=None, candidates=[])
    _install_stream(monkeypatch, [first, _USAGE_CHUNK])

    _ = [ev async for ev in google_sdk.stream(_req(), _RESOLVED)]

    assert rec.started == {}
    assert len(made) == 1
    assert made[0].started["name"] == "Gemini-generation"
    assert made[0].started["input_data"] == _req().messages
    assert made[0].ends == [(
        "finish",
        {
            "output": "안녕",
            "usage_details": {
                "input": 60, "input_cached_tokens": 40, "output": 20, "output_reasoning": 5, "total": 125,
            },
        },
    )]


async def test_stream_records_cancel_when_user_leaves(monkeypatch) -> None:
    """사용자가 도중에 끊으면 오류가 아니라 취소로 닫고 그때까지의 글을 싣는다."""
    made = _record_stream_generation(monkeypatch)
    first = SimpleNamespace(text="안", usage_metadata=None, model_version=None, candidates=[])
    _install_stream(monkeypatch, [first, _USAGE_CHUNK])

    gen = google_sdk.stream(_req(), _RESOLVED)
    assert await gen.__anext__() == TextDelta("안")
    await gen.aclose()

    assert made[0].ends == [("cancel", {"output": "안", "usage_details": None})]


async def test_stream_records_failure_midway(monkeypatch) -> None:
    """도중에 공급자 오류가 나면 실패로 한 번 닫고, 호출부에는 중립 예외가 간다."""
    made = _record_stream_generation(monkeypatch)
    first = SimpleNamespace(text="가", usage_metadata=None, model_version=None, candidates=[])
    _install_stream(monkeypatch, [first], error=_api_error(429, "rate limited"))

    with pytest.raises(LlmRateLimited):
        _ = [ev async for ev in google_sdk.stream(_req(), _RESOLVED)]

    assert [kind for kind, _ in made[0].ends] == ["fail"]


@pytest.mark.parametrize("ending", ["finish", "fail", "cancel"])
@pytest.mark.parametrize("has_chunks", [True, False])
async def test_stream_records_ttft_from_first_provider_chunk(monkeypatch, ending, has_chunks) -> None:
    updates = []
    span = SimpleNamespace(update=lambda **kw: updates.append(kw), end=lambda: None)
    monkeypatch.setattr(google_sdk, "start_stream_generation", lambda *a, **kw: lf._StreamGeneration(span))
    first = datetime(2026, 10, 22, tzinfo=timezone.utc)
    timestamps = []

    def now(tz):
        timestamps.append(tz)
        return first

    monkeypatch.setattr(lf, "datetime", SimpleNamespace(now=now))

    async def chunks():
        if has_chunks:
            # 빈 텍스트인 첫 조각도 OpenAI 자동 계측과 같은 기준으로 센다.
            yield SimpleNamespace(text="", usage_metadata=None, candidates=[])
            assert timestamps == [timezone.utc]
            yield SimpleNamespace(text="body", usage_metadata=None, candidates=[])
        if ending == "fail":
            raise _api_error(429, "rate limited")
        if ending == "cancel":
            raise asyncio.CancelledError()

    monkeypatch.setattr(google_sdk, "_client", lambda _: SimpleNamespace(aio=SimpleNamespace(
        models=SimpleNamespace(generate_content_stream=AsyncMock(return_value=chunks())),
    )))
    if ending == "finish":
        events = [event async for event in google_sdk.stream(_req(), _RESOLVED)]
        assert [event.text for event in events if isinstance(event, TextDelta)] == (["body"] if has_chunks else [])
    else:
        with pytest.raises(LlmRateLimited if ending == "fail" else asyncio.CancelledError):
            _ = [event async for event in google_sdk.stream(_req(), _RESOLVED)]

    assert len(updates) == 1
    if has_chunks:
        assert updates[0]["completion_start_time"] == first
        assert timestamps == [timezone.utc]
    else:
        assert "completion_start_time" not in updates[0]
        assert timestamps == []


@pytest.mark.parametrize("ending", ["completed", "closed", "error", "cancelled"])
@pytest.mark.parametrize("cleanup_failure", [None, "sdk", "http", "both"])
async def test_stream_closes_sdk_stream_on_every_exit(monkeypatch, ending: str, cleanup_failure: str | None) -> None:
    """상위 스트림 종료가 SDK 스트림의 명시적 정리까지 이어져야 한다."""
    waiting = asyncio.Event()

    class SdkStream:
        def __init__(self) -> None:
            self.reads = 0
            self.close_count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.reads += 1
            if self.reads == 1:
                return SimpleNamespace(text="본문", usage_metadata=None, model_version=None, candidates=[])
            if ending == "error":
                raise _api_error(429, "rate limited")
            if ending == "cancelled":
                waiting.set()
                await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.close_count += 1
            if cleanup_failure in {"sdk", "both"}:
                raise OSError("SDK cleanup failed")

    http_close = AsyncMock(side_effect=OSError("HTTP cleanup failed") if cleanup_failure in {"http", "both"} else None)
    sdk_stream = SdkStream()

    async def generate(**kwargs):
        await google_sdk._track_stream_response(SimpleNamespace(aclose=http_close))
        return sdk_stream
    monkeypatch.setattr(
        google_sdk, "_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=generate,
        ))),
    )
    made = _record_stream_generation(monkeypatch)
    stream = google_sdk.stream(_req(), _RESOLVED)
    assert await anext(stream) == TextDelta("본문")
    try:
        if ending == "completed":
            if cleanup_failure:
                with pytest.raises(LlmUnavailable):
                    _ = [event async for event in stream]
            else:
                events = [event async for event in stream]
                assert len(events) == 1 and isinstance(events[0], StreamCompleted)
        elif ending == "closed":
            await stream.aclose()
        elif ending == "error":
            with pytest.raises(LlmRateLimited):
                await anext(stream)
        else:
            task = asyncio.create_task(anext(stream))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert sdk_stream.close_count == 1
        http_close.assert_awaited_once()
        expected = {"completed": "finish", "closed": "cancel", "error": "fail", "cancelled": "cancel"}
        expected_end = "fail" if ending == "completed" and cleanup_failure else expected[ending]
        assert [kind for kind, _ in made[0].ends] == [expected_end]
    finally:
        await stream.aclose()


@pytest.mark.parametrize("ending", ["completed", "closed", "cancelled", "concurrent"])
async def test_real_google_sdk_closes_http_response(monkeypatch, ending: str) -> None:
    """SDK의 생성·파싱·스트림 계층은 그대로 두고 HTTP 전송만 대체한다."""
    waiting = asyncio.Event()
    responses: list[httpx.Response] = []

    class ResponseBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "본문"}]}}]}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            if ending == "cancelled":
                waiting.set()
                await asyncio.Event().wait()
            yield b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'

        async def aclose(self) -> None:
            self.closed = True

    bodies: list[ResponseBody] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = ResponseBody()
        bodies.append(body)
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        responses.append(response)
        return response

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        google_sdk.httpx, "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(google_sdk.registry, "credentials", lambda _: ProviderCredentials(
        api_key="test-key", base_url=None, api_key_env="GEMINI_API_KEY", base_url_env="GEMINI_API_URL",
    ))
    sdk_client = google_sdk._client(PROVIDER_GOOGLE)
    stream = google_sdk.stream(_req(), _RESOLVED)
    task = None
    peer = None
    try:
        assert await anext(stream) == TextDelta("본문")
        assert len(responses) == 1
        assert not responses[0].is_closed
        if ending == "completed":
            remaining = [event async for event in stream]
            assert isinstance(remaining[-1], StreamCompleted)
        elif ending == "closed":
            await stream.aclose()
        elif ending == "concurrent":
            peer = google_sdk.stream(_req(), _RESOLVED)
            assert await asyncio.create_task(anext(peer)) == TextDelta("본문")
            assert len(responses) == 2
            await stream.aclose()
            assert not responses[1].is_closed
            remaining = [event async for event in peer]
            assert isinstance(remaining[-1], StreamCompleted)
            assert responses[1].is_closed and bodies[1].closed
        else:
            task = asyncio.create_task(anext(stream))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # 테스트의 뒷정리가 연결을 닫기 전에 검사한다.
        assert responses[0].is_closed, "SDK HTTP response remains open after stream exit"
        assert bodies[0].closed, "HTTP response body was not closed"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        if peer is not None:
            await peer.aclose()
        for response in responses:
            await response.aclose()
        await sdk_client.aio.aclose()
        sdk_client.close()


# ── 거절 신호 정규화(KNK-1859) ─────────────────────────────────────────────────
def test_prompt_block_is_normalized_to_safety_finish_reason() -> None:
    """입력 자체가 안전 필터에 막히면 후보가 비고 prompt_feedback.block_reason만 온다.
    그대로 두면 빈 응답으로 보여 재호출이 나가므로 safety로 넘긴다."""
    resp = SimpleNamespace(
        text="",
        candidates=[],
        prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name="SAFETY")),
    )

    assert google_sdk._finish_reason_of(resp) == "safety"


def test_unspecified_block_reason_is_not_a_refusal() -> None:
    """block_reason이 '미지정'이면 차단이 아니다 — None 유지."""
    resp = SimpleNamespace(
        text="",
        candidates=[],
        prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name="BLOCKED_REASON_UNSPECIFIED")),
    )

    assert google_sdk._finish_reason_of(resp) is None


def test_candidate_finish_reason_wins_over_prompt_feedback() -> None:
    """후보가 있으면 후보의 종료 이유를 쓴다(정상 응답에 prompt_feedback이 함께 와도 stop)."""
    resp = _fake_response(finish_reason_name="STOP")
    resp.prompt_feedback = SimpleNamespace(block_reason=None)

    assert google_sdk._finish_reason_of(resp) == "stop"
