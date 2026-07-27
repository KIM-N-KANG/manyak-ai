"""OpenAI SDK 어댑터·통로 테스트 (KNK-671).

SDK는 목으로 세운다. **어댑터가 SDK에 넘기는 인자를 정확히 단언하는 것**이 이 파일의 핵심이다 —
모르는 인자를 몰래 붙이거나 필요한 인자를 빠뜨려도 라이브 호출 없이는 드러나지 않기 때문이다.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.core.config import Settings
from src.services import llm
from src.services.llm import openai_sdk, registry
from src.services.llm.base import (
    ADAPTER_OPENAI_SDK,
    PROVIDER_DEEPSEEK,
    LlmBadRequest,
    LlmConfigError,
    LlmRateLimited,
    LlmRequest,
    LlmTimeout,
    LlmUnavailable,
    ResolvedModel,
    StreamCompleted,
    TextDelta,
)
from src.services.llm.registry import ProviderCredentials

_FLASH = ResolvedModel(
    model="deepseek-v4-flash",
    provider=PROVIDER_DEEPSEEK,
    adapter=ADAPTER_OPENAI_SDK,
    use_thinking=False,
    supports_temperature=True,
)
# temperature를 안 받고 추론을 쓰는 다른 회사 모델(가상) — 인자 생략·문법 분기 확인용.
_STRICT = ResolvedModel(
    model="other-1",
    provider="other",
    adapter=ADAPTER_OPENAI_SDK,
    use_thinking=True,
    supports_temperature=False,
)


# ── 목 SDK ───────────────────────────────────────────────────────────────────
class _FakeCompletions:
    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self.captured: dict | None = None
        self._result = result
        self._error = error

    async def create(self, **kwargs):
        self.captured = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def _install(monkeypatch, completions: _FakeCompletions) -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(openai_sdk, "_client", lambda provider: client)


def _usage(prompt=100, completion=20, cache_hit=64):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, prompt_cache_hit_tokens=cache_hit
    )


def _response(content="본문", model="deepseek-v4-flash", usage=None, finish_reason="stop"):
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=usage or _usage())


def _req(**overrides) -> LlmRequest:
    values = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
    }
    return LlmRequest(**(values | overrides))


def _openai_error(kind: str) -> BaseException:
    request = httpx.Request("POST", "https://api.deepseek.com/v1")
    if kind == "timeout":
        from openai import APITimeoutError

        return APITimeoutError(request=request)
    if kind == "rate":
        from openai import RateLimitError

        return RateLimitError("rate", response=httpx.Response(429, request=request), body=None)
    if kind == "bad":
        from openai import BadRequestError

        return BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
    status_errors = {
        "auth": ("AuthenticationError", 401),
        "forbidden": ("PermissionDeniedError", 403),
        "not_found": ("NotFoundError", 404),
        "unprocessable": ("UnprocessableEntityError", 422),
        "server": ("InternalServerError", 500),
    }
    if kind in status_errors:
        import openai

        name, status = status_errors[kind]
        return getattr(openai, name)(
            kind, response=httpx.Response(status, request=request), body=None
        )
    from openai import APIConnectionError

    return APIConnectionError(request=request)


# ── 인자 조립 (정확 일치) ────────────────────────────────────────────────────
async def test_complete_sends_exact_kwargs(monkeypatch) -> None:
    """호출부가 준 값 + 등록부의 뜻이 이 SDK 문법으로 정확히 옮겨진다."""
    completions = _FakeCompletions(result=_response())
    _install(monkeypatch, completions)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    await openai_sdk.complete(
        _req(messages=messages, json_mode=True, temperature=0.75, max_tokens=6144, timeout=88.5),
        _FLASH,
    )

    assert completions.captured == {
        "model": "deepseek-v4-flash",
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.75,
        "max_tokens": 6144,
        "timeout": 88.5,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


async def test_complete_omits_absent_values(monkeypatch) -> None:
    """값이 없는 인자는 아예 넣지 않는다 — SDK 기본값에 맡긴다."""
    completions = _FakeCompletions(result=_response())
    _install(monkeypatch, completions)

    await openai_sdk.complete(_req(), _FLASH)

    assert set(completions.captured) == {"model", "messages", "extra_body"}


async def test_complete_drops_unsupported_temperature(monkeypatch) -> None:
    """모델이 temperature를 안 받으면 인자를 뺀다 — 다른 값으로 바꾸지 않는다."""
    completions = _FakeCompletions(result=_response(model="other-1"))
    _install(monkeypatch, completions)

    await openai_sdk.complete(_req(model="other-1", temperature=0.75), _STRICT)

    assert "temperature" not in completions.captured


async def test_complete_omits_thinking_when_model_uses_it(monkeypatch) -> None:
    """추론을 쓰는 모델에는 끄는 문법을 붙이지 않는다."""
    completions = _FakeCompletions(result=_response(model="deepseek-v4-flash"))
    _install(monkeypatch, completions)
    thinking_on = ResolvedModel(
        model="deepseek-v4-flash",
        provider=PROVIDER_DEEPSEEK,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=True,
        supports_temperature=True,
    )

    await openai_sdk.complete(_req(), thinking_on)

    assert "extra_body" not in completions.captured


async def test_unknown_provider_cannot_silently_ignore_thinking_off(monkeypatch) -> None:
    """문법을 모르는 공급자에 "추론 끄기"를 지시하면 거부한다.

    조용히 넘기면 등록부에 끄라고 적어둔 모델이 추론이 켜진 채 호출된다 — 등록부가 추론 모드를
    반드시 정하게 만든 취지가 무너진다.
    """
    _install(monkeypatch, _FakeCompletions(result=_response(model="other-1")))
    unknown = ResolvedModel(
        model="other-1",
        provider="other",
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,  # 끄라고 했는데 이 공급자의 문법을 모른다
        supports_temperature=False,
    )

    with pytest.raises(LlmConfigError) as exc_info:
        await openai_sdk.complete(_req(model="other-1"), unknown)

    assert "other" in str(exc_info.value)


async def test_registry_settings_are_not_mutated(monkeypatch) -> None:
    """어댑터가 넘기는 문법 dict는 매번 새로 만든다 — 등록부 값이 오염되지 않는다."""
    completions = _FakeCompletions(result=_response())
    _install(monkeypatch, completions)

    await openai_sdk.complete(_req(), _FLASH)
    completions.captured["extra_body"]["오염"] = True  # 넘긴 dict를 호출부가 고쳐도

    completions2 = _FakeCompletions(result=_response())
    _install(monkeypatch, completions2)
    await openai_sdk.complete(_req(), _FLASH)

    assert completions2.captured["extra_body"] == {"thinking": {"type": "disabled"}}


# ── 응답 해석 ────────────────────────────────────────────────────────────────
async def test_complete_maps_result_fields(monkeypatch) -> None:
    _install(monkeypatch, _FakeCompletions(result=_response(content="안녕", finish_reason="stop")))

    result = await openai_sdk.complete(_req(), _FLASH)

    assert result.text == "안녕"
    assert result.model == "deepseek-v4-flash"
    assert result.provider == PROVIDER_DEEPSEEK
    assert result.finish_reason == "stop"


async def test_usage_does_not_double_count_cache(monkeypatch) -> None:
    """prompt_tokens는 캐시 적중분을 이미 포함한 합계라 그대로 쓴다(더하면 부풀려진다)."""
    _install(monkeypatch, _FakeCompletions(result=_response(usage=_usage(100, 20, 64))))

    usage = (await openai_sdk.complete(_req(), _FLASH)).usage

    assert usage.input_tokens == 100  # 100 + 64가 아니다
    assert usage.output_tokens == 20
    assert usage.cache_read_input_tokens == 64


async def test_usage_missing_stays_none(monkeypatch) -> None:
    """usage가 없으면 0이 아니라 None으로 남긴다(백엔드 계약: 누락 시 null)."""
    response = _response()
    response.usage = None
    _install(monkeypatch, _FakeCompletions(result=response))

    usage = (await openai_sdk.complete(_req(), _FLASH)).usage

    assert usage.input_tokens is None
    assert usage.output_tokens is None


@pytest.mark.parametrize(
    "broken",
    [
        # 목록 자리에 목록이 아닌 것 / 글 자리에 글자가 아닌 것 — 모양을 나열해 막지 않고
        # "못 꺼내면 빈 글" 한 규칙으로 덮는다(경우가 끝이 없다).
        SimpleNamespace(choices={"0": "이상함"}, model="deepseek-v4-flash", usage=None),
        SimpleNamespace(choices=1, model="deepseek-v4-flash", usage=None),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=[{"type": "text", "text": "본문"}]),
                    finish_reason=None,
                )
            ],
            model="deepseek-v4-flash",
            usage=None,
        ),
        SimpleNamespace(choices=[], model="deepseek-v4-flash", usage=None),
        SimpleNamespace(
            choices=[SimpleNamespace(message=None, finish_reason=None)],
            model="deepseek-v4-flash",
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None), finish_reason=None)],
            model="deepseek-v4-flash",
            usage=None,
        ),
    ],
)
async def test_broken_response_returns_empty_text(monkeypatch, broken) -> None:
    """빈 choices·message 없음·빈 본문은 예외가 아니라 빈 글이다.

    여기서 예외를 던지면 스토리라인 invalid 재호출(KNK-312)이 전송 오류 경로로 새서 사라진다.
    """
    _install(monkeypatch, _FakeCompletions(result=broken))

    result = await openai_sdk.complete(_req(), _FLASH)

    assert result.text == ""


async def test_model_falls_back_to_requested_name(monkeypatch) -> None:
    """응답에 모델명이 비어 오면 요청에 쓴 이름으로 채운다(빈 값은 meta 조립에서 터진다)."""
    _install(monkeypatch, _FakeCompletions(result=_response(model=None)))

    result = await openai_sdk.complete(_req(model="deepseek-v4-flash"), _FLASH)

    assert result.model == "deepseek-v4-flash"


# ── 예외 번역 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("timeout", LlmTimeout),
        ("rate", LlmRateLimited),
        ("bad", LlmBadRequest),
        ("connection", LlmUnavailable),
        # 4xx라고 다 bad_request가 아니다 — 400만 요청 오류이고 나머지는 일시 장애로 묶는다
        # (지금 classify_error_code 분류·502 문구를 그대로 보존).
        ("auth", LlmUnavailable),
        ("forbidden", LlmUnavailable),
        ("not_found", LlmUnavailable),
        ("unprocessable", LlmUnavailable),
        ("server", LlmUnavailable),
    ],
)
async def test_translates_sdk_errors(monkeypatch, kind: str, expected: type) -> None:
    _install(monkeypatch, _FakeCompletions(error=_openai_error(kind)))

    with pytest.raises(expected) as exc_info:
        await openai_sdk.complete(_req(), _FLASH)

    # 실패 경로엔 결과가 없으므로 예외가 provider·model의 유일한 출처다.
    assert exc_info.value.provider == PROVIDER_DEEPSEEK
    assert exc_info.value.model == "deepseek-v4-flash"


async def test_timeout_is_checked_before_connection_error(monkeypatch) -> None:
    """APITimeoutError는 APIConnectionError의 하위 클래스 — 순서가 뒤집히면 타임아웃이 사라진다."""
    _install(monkeypatch, _FakeCompletions(error=_openai_error("timeout")))

    with pytest.raises(LlmTimeout):
        await openai_sdk.complete(_req(), _FLASH)


# ── 스트리밍 ─────────────────────────────────────────────────────────────────
def _chunk(content=None, model="deepseek-v4-flash", usage=None, finish_reason=None):
    choices = []
    if content is not None or finish_reason is not None:
        choices = [SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, model=model, usage=usage)


class _FakeStream:
    """실제 `openai.AsyncStream`의 모양을 흉내 낸다 — 반복 가능하고 `close()`로 닫힌다.

    async generator로 대신하면 `close()`가 없어(그쪽은 `aclose()`) 어댑터가 스트림을 닫는지
    검증할 수 없다. 목이 실제 타입과 다르면 통과해도 아무것도 증명하지 못한다.
    """

    def __init__(self, chunks: list, error: BaseException | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self.closed = True


def _agen(chunks: list, error: BaseException | None = None) -> _FakeStream:
    return _FakeStream(chunks, error)


async def test_stream_yields_deltas_then_completed(monkeypatch) -> None:
    chunks = [
        _chunk("안"),
        _chunk("녕"),
        _chunk(finish_reason="stop"),
        _chunk(usage=_usage(100, 20, 64)),  # usage 전용 마지막 청크(choices 비어 있음)
    ]
    completions = _FakeCompletions(result=_agen(chunks))
    _install(monkeypatch, completions)

    events = [ev async for ev in openai_sdk.stream(_req(), _FLASH)]

    assert events[:2] == [TextDelta("안"), TextDelta("녕")]
    # 이벤트 구성 전체를 고정한다 — 마지막 것만 보면 종료 이벤트가 두 번 나가도 통과한다
    # (Codex 리뷰에서 변이로 확인). 종료는 맨 끝에 정확히 한 번이다.
    assert [type(ev) for ev in events] == [TextDelta, TextDelta, StreamCompleted]
    assert sum(isinstance(ev, StreamCompleted) for ev in events) == 1
    assert events[-1].usage.input_tokens == 100
    assert events[-1].finish_reason == "stop"
    assert events[-1].provider == PROVIDER_DEEPSEEK


@pytest.mark.parametrize(
    "weird",
    [
        [{"type": "text", "text": "안녕"}],  # 조각을 블록 목록으로 주는 공급자
        {"text": "안녕"},
        123,
        object(),
    ],
)
async def test_stream_drops_non_text_delta(monkeypatch, weird: object) -> None:
    """글자가 아닌 조각은 흘리지 않는다 — 그대로 내보내면 화면에 그 표기가 뜨거나 이어붙이다 터진다."""
    chunks = [_chunk("안"), _chunk(weird), _chunk("녕"), _chunk(finish_reason="stop")]
    _install(monkeypatch, _FakeCompletions(result=_agen(chunks)))

    events = [ev async for ev in openai_sdk.stream(_req(), _FLASH)]

    assert [ev for ev in events if isinstance(ev, TextDelta)] == [TextDelta("안"), TextDelta("녕")]
    assert events[-1].finish_reason == "stop"


async def test_stream_ignores_non_text_finish_reason(monkeypatch) -> None:
    """종료 이유도 글자일 때만 쓴다 — 이상한 값이 응답 meta에 그대로 실려 나가지 않게."""
    chunks = [_chunk("가"), _chunk(finish_reason=["stop"])]
    _install(monkeypatch, _FakeCompletions(result=_agen(chunks)))

    events = [ev async for ev in openai_sdk.stream(_req(), _FLASH)]

    assert events[-1].finish_reason is None


async def test_stream_requests_usage_in_last_chunk(monkeypatch) -> None:
    """토큰 로깅을 위해 usage 동봉을 항상 요청한다."""
    completions = _FakeCompletions(result=_agen([_chunk("가")]))
    _install(monkeypatch, completions)

    [ev async for ev in openai_sdk.stream(_req(), _FLASH)]

    assert completions.captured["stream"] is True
    assert completions.captured["stream_options"] == {"include_usage": True}


async def test_stream_error_after_deltas_becomes_neutral_error(monkeypatch) -> None:
    """일부를 흘려보낸 뒤 실패해도 중립 예외로 바뀐다 — 호출부가 SSE error로 옮긴다."""
    completions = _FakeCompletions(result=_agen([_chunk("가")], error=_openai_error("rate")))
    _install(monkeypatch, completions)

    seen: list = []
    with pytest.raises(LlmRateLimited):
        async for ev in openai_sdk.stream(_req(), _FLASH):
            seen.append(ev)

    assert seen == [TextDelta("가")]  # 이미 보낸 조각은 그대로 나갔다


async def test_stream_failure_before_first_chunk(monkeypatch) -> None:
    """조각이 하나도 오기 전에 실패해도 중립 예외다.

    async generator라 호출한 자리가 아니라 **첫 조각을 받으려는 순간** 드러난다.
    """
    _install(monkeypatch, _FakeCompletions(error=_openai_error("timeout")))
    events = openai_sdk.stream(_req(), _FLASH)  # 여기서는 아직 안 터진다

    with pytest.raises(LlmTimeout):
        await events.__anext__()


@pytest.mark.parametrize(
    "escaping",
    [
        httpx.RemoteProtocolError("연결이 끊겼습니다"),
        json.JSONDecodeError("깨진 SSE 줄", "{", 0),
    ],
)
async def test_stream_non_sdk_error_is_also_folded(monkeypatch, escaping: Exception) -> None:
    """SDK 밖 오류도 중립 예외로 접는다.

    SDK의 스트림 반복은 try/finally로만 감싸져(except 없음) 연결 끊김·깨진 SSE 줄이 번역 없이
    통과한다. 그대로 두면 채팅은 error 이벤트도 못 내고 끊기고, 선택지·판정은 흡수에 실패해 500이 된다.
    """
    completions = _FakeCompletions(result=_agen([_chunk("가")], error=escaping))
    _install(monkeypatch, completions)

    with pytest.raises(LlmUnavailable) as exc_info:
        async for _ in openai_sdk.stream(_req(), _FLASH):
            pass

    assert exc_info.value.provider == PROVIDER_DEEPSEEK
    assert type(escaping).__name__ in str(exc_info.value)


async def test_stream_read_timeout_stays_a_timeout(monkeypatch) -> None:
    """스트림이 열린 뒤의 읽기 시간 초과도 '시간 초과'다.

    SDK는 요청 단계의 초과만 자기 예외로 접고 반복 중의 것은 그대로 올려보낸다 — 접는 자리를
    나누지 않으면 같은 시간 초과가 발생 시점에 따라 다른 코드·다른 사용자 문구가 된다.
    """
    completions = _FakeCompletions(
        result=_agen([_chunk("가")], error=httpx.ReadTimeout("응답이 멈췄습니다"))
    )
    _install(monkeypatch, completions)

    with pytest.raises(LlmTimeout):
        async for _ in openai_sdk.stream(_req(), _FLASH):
            pass


async def test_close_failure_does_not_mask_the_real_error(monkeypatch) -> None:
    """정리하다 실패해도 원래 오류가 그대로 올라온다 — 정리 예외가 원인을 덮으면 안 된다."""

    class _UncloseableStream(_FakeStream):
        async def close(self) -> None:
            raise RuntimeError("정리 실패")

    _install(
        monkeypatch,
        _FakeCompletions(result=_UncloseableStream([_chunk("가")], error=_openai_error("rate"))),
    )

    with pytest.raises(LlmRateLimited):  # RuntimeError가 아니라 원래 오류
        async for _ in openai_sdk.stream(_req(), _FLASH):
            pass


async def test_close_failure_does_not_break_normal_finish(monkeypatch) -> None:
    """정상 종료도 마찬가지다 — 정리 실패로 완료 이벤트가 사라지면 안 된다."""

    class _UncloseableStream(_FakeStream):
        async def close(self) -> None:
            raise RuntimeError("정리 실패")

    _install(monkeypatch, _FakeCompletions(result=_UncloseableStream([_chunk("가")])))

    events = [ev async for ev in openai_sdk.stream(_req(), _FLASH)]

    assert isinstance(events[-1], StreamCompleted)


async def test_stream_cancellation_is_not_an_error(monkeypatch) -> None:
    """사용자 연결 취소는 오류가 아니다 — 중립 예외로 바꾸지 않고 그대로 통과시킨다."""
    completions = _FakeCompletions(result=_agen([_chunk("가")], error=asyncio.CancelledError()))
    _install(monkeypatch, completions)

    with pytest.raises(asyncio.CancelledError):
        async for _ in openai_sdk.stream(_req(), _FLASH):
            pass


async def test_stream_is_closed_when_consumer_leaves_early(monkeypatch) -> None:
    """사용자가 중간에 떠나면 스트림을 닫아 커넥션을 반납한다.

    `break`만으로는 부족하다 — 파이썬은 제너레이터를 그 자리에서 정리하지 않고 나중에 회수한다.
    실제 SSE 경로는 연결이 끊기면 태스크가 취소되면서 제너레이터가 닫히므로, 그 정리 시점을
    여기서 재현한다(정리될 때 커넥션이 반납되는지가 확인 대상이다).
    """
    fake_stream = _FakeStream([_chunk("가"), _chunk("나")])
    _install(monkeypatch, _FakeCompletions(result=fake_stream))
    events = openai_sdk.stream(_req(), _FLASH)

    async for _ in events:
        break  # 첫 조각만 받고 떠난다
    await events.aclose()  # 취소·회수 시점

    assert fake_stream.closed is True


async def test_stream_is_closed_after_normal_finish(monkeypatch) -> None:
    fake_stream = _FakeStream([_chunk("가")])
    _install(monkeypatch, _FakeCompletions(result=fake_stream))

    [ev async for ev in openai_sdk.stream(_req(), _FLASH)]

    assert fake_stream.closed is True


# ── 클라이언트 생성·재사용 ────────────────────────────────────────────────────
@pytest.fixture
def _isolated_client_cache():
    """캐시를 비우고 테스트 후 원상복구한다 — 테스트가 서로의 클라이언트를 물려받지 않게."""
    saved = dict(openai_sdk._clients)
    openai_sdk._clients.clear()
    yield
    openai_sdk._clients.clear()
    openai_sdk._clients.update(saved)


def _creds(api_key: str = "test-key", base_url: str = "https://example.invalid"):
    return ProviderCredentials(
        api_key=api_key,
        base_url=base_url,
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_API_URL",
    )


def test_client_is_built_with_declared_arguments(monkeypatch, _isolated_client_cache) -> None:
    """클라이언트 생성 인자(키·주소·재시도 횟수)를 실제로 단언한다.

    특히 **재시도 횟수**가 이 테스트의 이유다. 전에는 숫자를 안 적고 SDK 기본값에 기대서,
    0으로 바꿔도 테스트가 전부 통과했다(Codex 변이 시험). 전송 실패 재시도가 조용히 사라지면
    일시적 연결 끊김·429의 성공률이 떨어지는데 CI가 못 잡는다.

    timeout은 여기서 넣지 않는다 — 호출마다 다르고(LlmRequest.timeout) 요청 인자로 나간다.
    """
    built: dict = {}

    class _SpyClient:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setattr(openai_sdk, "AsyncOpenAI", _SpyClient)
    monkeypatch.setattr(
        registry, "credentials", lambda provider: _creds(api_key="k", base_url="https://x.invalid")
    )

    openai_sdk._client(PROVIDER_DEEPSEEK)

    assert built == {"api_key": "k", "base_url": "https://x.invalid", "max_retries": 2}


def test_client_is_reused_per_provider(monkeypatch, _isolated_client_cache) -> None:
    """호출마다 새로 만들면 커넥션 풀·TLS 세션이 매번 버려진다."""
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds())

    first = openai_sdk._client(PROVIDER_DEEPSEEK)
    second = openai_sdk._client(PROVIDER_DEEPSEEK)

    assert first is second


def test_client_is_rebuilt_when_settings_change(monkeypatch, _isolated_client_cache) -> None:
    """주소·키가 바뀌면 새 클라이언트를 만든다(캐시 이름표가 설정 변화를 감지해야 한다)."""
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key="key-1"))
    first = openai_sdk._client(PROVIDER_DEEPSEEK)

    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key="key-2"))
    second = openai_sdk._client(PROVIDER_DEEPSEEK)

    assert first is not second


def test_cache_label_never_holds_the_raw_key(monkeypatch, _isolated_client_cache) -> None:
    """캐시 이름표에 키 원문을 넣지 않는다.

    Sentry는 오류 시 그 시점의 지역변수 값을 함께 싣는다 — 이름표에 키가 있으면 그대로 전송된다.
    """
    secret = "sk-super-secret-value"
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key=secret))

    openai_sdk._client(PROVIDER_DEEPSEEK)

    assert all(secret not in str(label) for label in openai_sdk._clients)


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_key_is_a_config_error(monkeypatch, _isolated_client_cache, blank: str) -> None:
    """키가 비면(공백 포함) 설정 오류로 막는다 — SDK 생성자 예외가 번역 없이 새면 안 된다.

    공백만 있는 키는 SDK가 통과시키므로 여기서 함께 막아 기동 검사(`validate_selected_models`)와
    판정을 일치시킨다.
    """
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key=blank))

    with pytest.raises(LlmConfigError) as exc_info:
        openai_sdk._client(PROVIDER_DEEPSEEK)

    assert "DEEPSEEK_API_KEY" in str(exc_info.value)


# ── 통로 배선 ────────────────────────────────────────────────────────────────
async def test_gateway_complete_routes_to_adapter(monkeypatch) -> None:
    completions = _FakeCompletions(result=_response())
    _install(monkeypatch, completions)

    result = await llm.complete(_req(json_mode=True))

    assert result.provider == PROVIDER_DEEPSEEK
    assert completions.captured["model"] == "deepseek-v4-flash"


async def test_gateway_stream_routes_to_adapter(monkeypatch) -> None:
    _install(monkeypatch, _FakeCompletions(result=_agen([_chunk("가")])))

    events = [ev async for ev in llm.stream(_req())]

    assert events[0] == TextDelta("가")


def test_gateway_rejects_unregistered_model_immediately() -> None:
    """미등록 모델은 첫 조각을 기다리기 전에, 호출한 자리에서 드러난다."""
    with pytest.raises(LlmConfigError):
        llm.stream(_req(model="deepseek-v9-imaginary"))


def test_validate_startup_passes_with_registered_models() -> None:
    """지금 설정(DeepSeek 2종)은 기동 검사를 통과한다 — CI·팀 로컬이 그대로 뜬다."""
    llm.validate_startup()  # 예외 없이 통과


def test_validate_startup_rejects_model_the_adapter_cannot_express(monkeypatch) -> None:
    """어댑터가 표현 못 하는 설정은 **기동에서** 막는다 — 첫 사용자 요청이 아니라.

    등록·키 검사만 하면 이런 모델이 통과해, 첫 요청에서 LlmConfigError가 난다. 그 예외는
    LlmError가 아니라 선택지 폴백·판정 null 흡수 경로를 관통해 500이 된다.
    """
    unsupported = ResolvedModel(
        model="gpt-x",
        provider="openai",  # 키는 있다고 치지만
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,  # 이 공급자의 "추론 끄기" 문법을 어댑터가 모른다
        supports_temperature=True,
    )
    monkeypatch.setitem(registry._REGISTRY, "gpt-x", unsupported)
    monkeypatch.setattr(
        registry, "settings", Settings(_env_file=None, deepseek_api_key="k", chat_model="gpt-x")
    )
    monkeypatch.setattr(
        registry,
        "credentials",
        lambda provider: ProviderCredentials(
            api_key="key",
            base_url="https://example.invalid",
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_API_URL",
        ),
    )

    with pytest.raises(LlmConfigError) as exc_info:
        llm.validate_startup()

    assert "CHAT_MODEL" in str(exc_info.value)


def test_validate_startup_names_the_env_for_missing_adapter(monkeypatch) -> None:
    """어댑터 코드가 없어 기동이 막힐 때도 어느 env를 고칠지 알려준다.

    이 메시지만 보고 STORYLINES_MODEL·STORY_COMPILE_MODEL·CHAT_MODEL 중 무엇을 되돌릴지
    판단해야 한다 — 배포가 실패해 서버가 내려간 상황이라 추적할 시간이 없다.
    """
    future = ResolvedModel(
        model="claude-sonnet-5",
        provider="anthropic",
        adapter="anthropic_sdk",  # 아직 이 어댑터 코드가 없다(KNK-675)
        use_thinking=False,
        supports_temperature=False,
    )
    monkeypatch.setitem(registry._REGISTRY, "claude-sonnet-5", future)
    monkeypatch.setattr(
        registry,
        "settings",
        Settings(_env_file=None, deepseek_api_key="k", storylines_model="claude-sonnet-5"),
    )
    # 키·주소 검사는 통과시킨다 — 여기서 보려는 것은 그다음 단계인 어댑터 선택 실패다.
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds())

    with pytest.raises(LlmConfigError) as exc_info:
        llm.validate_startup()

    message = str(exc_info.value)
    assert "STORYLINES_MODEL" in message
    assert "anthropic_sdk" in message


def test_gateway_does_not_import_adapter_at_module_load() -> None:
    """통로 모듈은 어댑터를 미리 불러오지 않는다 — 순환 import를 만들지 않기 위해.

    `src.services.llm`을 맨 위에서 어댑터까지 끌어오면, 어댑터가 `src.core.sentry`를 쓰는
    순간(KNK-674) sentry → llm.base → llm → 어댑터 → sentry로 고리가 닫혀 import가 깨진다.

    새 파이썬 프로세스에서 확인한다. 이 프로세스에서는 앞선 테스트가 이미 어댑터를 불러왔을
    수 있어(한 번 불러오면 부모 패키지에 이름이 붙는다) 실행 순서에 따라 결과가 달라진다.
    """
    root = Path(__file__).resolve().parents[2]
    probe = (
        "import sys, src.services.llm;"
        " sys.exit(1 if 'src.services.llm.openai_sdk' in sys.modules else 0)"
    )

    done = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True)

    assert done.returncode == 0, f"통로 import가 어댑터까지 끌어왔다: {done.stderr.decode()}"


async def test_gateway_rejects_adapter_without_code(monkeypatch) -> None:
    """등록은 됐지만 처리할 어댑터 코드가 없으면 조용히 넘어가지 않는다(KNK-675 대비 가드)."""
    future = ResolvedModel(
        model="claude-sonnet-5",
        provider="anthropic",
        adapter="anthropic_sdk",
        use_thinking=False,
        supports_temperature=False,
    )
    monkeypatch.setattr(llm.registry, "resolve", lambda model: future)

    with pytest.raises(LlmConfigError) as exc_info:
        await llm.complete(_req(model="claude-sonnet-5"))

    assert "anthropic_sdk" in str(exc_info.value)
