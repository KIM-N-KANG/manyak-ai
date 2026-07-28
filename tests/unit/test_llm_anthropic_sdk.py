"""Anthropic SDK 어댑터 테스트 (KNK-676).

**라이브 호출은 없다.** 그래서 이 파일이 증명하는 것은 전부 "가짜 SDK가 진짜와 같은 모양인가"에
달려 있다. 이 레포는 그 함정을 이미 한 번 밟았다(`tests/conftest.py`의 `FakeStream` 주석 —
목이 실제 타입과 다르면 통과해도 아무것도 증명하지 못한다).

그래서 **응답과 예외를 손으로 만들지 않고 설치된 `anthropic` SDK의 실제 타입으로 만든다.**
필드 이름이 틀리면 테스트를 짜는 단계에서 터진다. 예외는 응답 껍데기가 깨진 경우만 손으로
만든다 — 그건 정의상 정상 SDK 타입이 아니다.
"""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from anthropic import NOT_GIVEN, AsyncAnthropic
from anthropic.types import Message as AnthropicMessage
from anthropic.types import TextDelta as AnthropicTextDelta
from anthropic.types import (
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta as AnthropicStopDelta

from src.core.config import Settings
from src.services import llm
from src.services.llm import anthropic_sdk, openai_sdk, registry
from src.services.llm.base import (
    ADAPTER_ANTHROPIC_SDK,
    PROVIDER_ANTHROPIC,
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

# 등록부에 실제로 올린 모델이 아니다 — 어댑터는 모델 이름을 몰라야 하므로, 시험용 이름으로
# 뜻만 바꿔가며 확인한다. 진짜 모델 등록은 이 티켓 범위 밖이다.
_THINKING = ResolvedModel(
    model="anthropic-test-model",
    provider=PROVIDER_ANTHROPIC,
    adapter=ADAPTER_ANTHROPIC_SDK,
    use_thinking=True,
    supports_temperature=True,
)
_NO_THINKING = ResolvedModel(
    model="anthropic-test-model",
    provider=PROVIDER_ANTHROPIC,
    adapter=ADAPTER_ANTHROPIC_SDK,
    use_thinking=False,
    supports_temperature=True,
)
# temperature를 안 받는 모델(예: Sonnet 5) — 인자 생략 확인용.
_STRICT = ResolvedModel(
    model="anthropic-strict-model",
    provider=PROVIDER_ANTHROPIC,
    adapter=ADAPTER_ANTHROPIC_SDK,
    use_thinking=True,
    supports_temperature=False,
)


# ── 목 SDK ───────────────────────────────────────────────────────────────────
class _FakeMessages:
    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self.captured: dict | None = None
        self._result = result
        self._error = error

    async def create(self, **kwargs):
        self.captured = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def _install(monkeypatch, messages: _FakeMessages) -> None:
    client = SimpleNamespace(messages=messages)
    monkeypatch.setattr(anthropic_sdk, "_client", lambda provider: client)


def _usage(input_tokens=100, output_tokens=20, creation=30, read=40) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=creation,
        cache_read_input_tokens=read,
    )


def _response(content=None, model="anthropic-test-model", usage=None, stop_reason="end_turn"):
    return AnthropicMessage(
        id="msg_1",
        content=content if content is not None else [TextBlock(type="text", text="본문")],
        model=model,
        role="assistant",
        type="message",
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
    )


def _req(**overrides) -> LlmRequest:
    # max_tokens를 기본값에 넣는다 — 이 어댑터의 단발 호출에 필수라
    # (`test_complete_requires_max_tokens` 참조) 빼면 모든 테스트가 그 가드에 걸린다.
    values = {
        "model": "anthropic-test-model",
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        "max_tokens": 16,
    }
    return LlmRequest(**(values | overrides))


def _code_of(source: str) -> str:
    """설명 글(주석·docstring)을 뺀 **실행 코드만** 남긴다.

    설명에 용도 이름이 나오는 것은 괜찮다 — "무엇을 고쳐라" 안내는 오히려 있어야 한다.
    문제는 코드가 그 이름으로 분기하거나 그 이름을 들고 다니는 것이다.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _anthropic_error(kind: str) -> BaseException:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    if kind == "timeout":
        from anthropic import APITimeoutError

        return APITimeoutError(request=request)
    if kind == "rate":
        from anthropic import RateLimitError

        return RateLimitError("rate", response=httpx.Response(429, request=request), body=None)
    if kind == "bad":
        from anthropic import BadRequestError

        return BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
    status_errors = {
        "auth": ("AuthenticationError", 401),
        "forbidden": ("PermissionDeniedError", 403),
        "not_found": ("NotFoundError", 404),
        "unprocessable": ("UnprocessableEntityError", 422),
        "server": ("InternalServerError", 500),
    }
    if kind in status_errors:
        import anthropic

        name, status = status_errors[kind]
        return getattr(anthropic, name)(
            kind, response=httpx.Response(status, request=request), body=None
        )
    from anthropic import APIConnectionError

    return APIConnectionError(request=request)


# ── 인자 조립 (정확 일치) ────────────────────────────────────────────────────
async def test_complete_sends_exact_kwargs(monkeypatch) -> None:
    """호출부가 준 값 + 등록부의 뜻이 이 회사 문법으로 정확히 옮겨진다.

    dict 전체를 비교한다 — 부분 단언만 하면 모르는 인자를 몰래 하나 더 붙여도 통과한다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(
        _req(temperature=0.75, max_tokens=6144, timeout=88.5),
        _NO_THINKING,
    )

    assert messages.captured == {
        "model": "anthropic-test-model",
        # system이 빠져나갔으므로 user만 남는다
        "messages": [{"role": "user", "content": "u"}],
        "system": [
            {"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}},
        ],
        "max_tokens": 6144,
        "thinking": {"type": "disabled"},
        "temperature": 0.75,
        "timeout": 88.5,
    }


async def test_complete_omits_absent_values(monkeypatch) -> None:
    """값이 없는 인자는 넣지 않는다.

    max_tokens는 이 어댑터의 단발 호출에서 필수라 최소 인자에도 들어간다(아래 가드 테스트 참조).
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(_req(), _THINKING)

    assert set(messages.captured) == {"model", "messages", "system", "max_tokens", "thinking"}


async def test_complete_requires_max_tokens(monkeypatch) -> None:
    """단발 호출에 max_tokens가 없으면 호출 전에 막는다.

    **이 버그는 가짜 SDK로는 드러나지 않는다** — 터지는 곳이 SDK 내부이기 때문이다.
    이 SDK는 스트리밍이 아니고 timeout도 안 준 호출에서 응답 대기 시간을 max_tokens로
    계산하는데(`maximum_time * max_tokens / 128_000`), 거기에 "값 없음" 표식이 들어가면
    TypeError가 난다. 그 예외는 SDK 예외가 아니라 번역되지 않고 500으로 새어 나간다.
    실제로 KNK-676 작업 중 기존 기동 검사 테스트가 이 TypeError로 터져서 발견됐다.

    그래서 목을 심지 않는다 — 가드가 **호출을 시작하기 전에** 막는지를 본다. 가드를 지우면
    실패가 SDK 쪽으로 넘어가 메시지가 달라지므로 아래 단언이 깨진다.
    """
    with pytest.raises(LlmConfigError) as exc_info:
        await anthropic_sdk.complete(_req(max_tokens=None), _THINKING)

    assert "max_tokens" in str(exc_info.value)


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [(_THINKING, {"type": "adaptive"}), (_NO_THINKING, {"type": "disabled"})],
)
async def test_thinking_is_always_explicit(monkeypatch, resolved, expected) -> None:
    """추론 켬/끔을 **생략하지 않고 언제나 명시한다**.

    이 회사는 모델마다 추론 기본값이 다르다. 켤 때 인자를 생략하면 기본이 꺼진 모델에서는
    등록부에 "켬"이라고 적어둔 채로 꺼진 호출이 나가는데, 오류가 없어 드러나지 않는다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(_req(), resolved)

    assert messages.captured["thinking"] == expected


async def test_complete_drops_unsupported_temperature(monkeypatch) -> None:
    """모델이 temperature를 안 받으면 인자를 뺀다 — 다른 값으로 바꾸지 않는다."""
    messages = _FakeMessages(result=_response(model="anthropic-strict-model"))
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(_req(temperature=0.75), _STRICT)

    assert "temperature" not in messages.captured


async def test_json_mode_is_dropped_with_a_warning(monkeypatch, caplog) -> None:
    """json_mode는 옮길 수 없어 뺀다 — 다만 조용히 빼지 않는다.

    이 회사는 JSON 강제에 스키마 전체를 요구하는데(`output_config.format.schema`가 필수)
    요청에는 그것을 담을 칸이 없다. 형식 준수가 프롬프트 지시에만 의존하게 되므로, 나중에
    JSON 파싱이 실패했을 때 원인을 로그에서 찾을 수 있어야 한다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    with caplog.at_level("WARNING"):
        await anthropic_sdk.complete(_req(json_mode=True), _THINKING)

    # OpenAI 계열의 response_format이 그대로 새어 나가지 않는지도 함께 고정한다.
    assert "response_format" not in messages.captured
    assert "output_config" not in messages.captured
    assert "json_mode" in caplog.text


async def test_registry_values_are_not_mutated(monkeypatch) -> None:
    """어댑터가 넘기는 문법 dict는 매번 새로 만든다 — 등록부 값이 오염되지 않는다."""
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(_req(), _NO_THINKING)
    messages.captured["thinking"]["오염"] = True  # 넘긴 dict를 받는 쪽이 고쳐도

    messages2 = _FakeMessages(result=_response())
    _install(monkeypatch, messages2)
    await anthropic_sdk.complete(_req(), _NO_THINKING)

    assert messages2.captured["thinking"] == {"type": "disabled"}


# ── system 분리 ──────────────────────────────────────────────────────────────
async def test_leading_systems_move_up_and_only_the_last_block_is_marked(monkeypatch) -> None:
    """맨 앞 지시문은 별도 칸으로 올라가고, **캐시 표시는 마지막 블록에만** 붙는다.

    앞 블록에 붙이면 그 지점까지만 캐시 대상이 돼 뒤가 빠진다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(
        _req(
            messages=[
                {"role": "system", "content": "앞1"},
                {"role": "system", "content": "앞2"},
                {"role": "user", "content": "u"},
            ]
        ),
        _THINKING,
    )

    assert messages.captured["system"] == [
        {"type": "text", "text": "앞1"},
        {"type": "text", "text": "앞2", "cache_control": {"type": "ephemeral"}},
    ]
    assert messages.captured["messages"] == [{"role": "user", "content": "u"}]


async def test_non_leading_system_is_dropped_with_a_warning(monkeypatch, caplog) -> None:
    """맨 앞이 아닌 자리의 지시문은 버리고 **경고를 남긴다**(감수한 위험).

    채팅 본문이 뒤에 두는 Depth·PHI가 여기 해당한다. PHI는 안전 가드레일이라, 조용히
    버리면 서버도 채팅도 정상인데 안전 지시만 없는 상태가 흔적 없이 성립한다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    with caplog.at_level("WARNING"):
        await anthropic_sdk.complete(
            _req(
                messages=[
                    {"role": "system", "content": "앞"},
                    {"role": "user", "content": "u"},
                    {"role": "system", "content": "Depth"},
                    {"role": "system", "content": "PHI"},
                ]
            ),
            _THINKING,
        )

    assert messages.captured["system"] == [
        {"type": "text", "text": "앞", "cache_control": {"type": "ephemeral"}}
    ]
    assert messages.captured["messages"] == [{"role": "user", "content": "u"}]
    # **몇 개를 버렸는지가 로그에 있어야 한다** — "뭔가 버렸다"만으로는 Depth 하나가 빠진 것과
    # Depth·PHI 둘 다 빠진 것을 구분할 수 없다.
    assert "2개" in caplog.text


@pytest.mark.parametrize(
    "system_message",
    [
        {"role": "system", "content": ""},
        {"role": "system", "content": "   "},
        {"role": "system"},  # content 키 자체가 없다
    ],
)
async def test_empty_system_is_not_sent(monkeypatch, system_message) -> None:
    """내용이 빈 지시문은 아예 안 보낸다(KNK-676 리뷰 P3).

    보내면 빈 글 블록에 캐시 표시까지 붙어 나가고 이 공급자는 400으로 거부한다. 같은 입력을
    받아주는 OpenAI 경로와 동작이 갈리는 것도 문제다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(
        _req(messages=[system_message, {"role": "user", "content": "u"}]), _THINKING
    )

    assert "system" not in messages.captured
    assert messages.captured["messages"] == [{"role": "user", "content": "u"}]


async def test_empty_system_does_not_swallow_a_real_one(monkeypatch) -> None:
    """빈 지시문만 빼고 내용 있는 것은 남긴다 — 캐시 표시는 남은 마지막 블록에 붙는다.

    짝으로 확인한다. 윗 테스트만 보면 "지시문을 전부 버리는" 코드도 통과한다.
    """
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(
        _req(
            messages=[
                {"role": "system", "content": ""},
                {"role": "system", "content": "진짜 지시"},
                {"role": "user", "content": "u"},
            ]
        ),
        _THINKING,
    )

    assert messages.captured["system"] == [
        {"type": "text", "text": "진짜 지시", "cache_control": {"type": "ephemeral"}}
    ]


async def test_no_system_omits_the_field(monkeypatch) -> None:
    """지시문이 없으면 그 인자를 아예 넣지 않는다(빈 목록을 보내지 않는다)."""
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    await anthropic_sdk.complete(_req(messages=[{"role": "user", "content": "u"}]), _THINKING)

    assert "system" not in messages.captured


# ── 토큰 ─────────────────────────────────────────────────────────────────────
async def test_input_tokens_sum_the_three_parts(monkeypatch) -> None:
    """입력 토큰은 일반+캐시 생성+캐시 읽기의 합이다.

    합치지 않으면 캐시가 잘 걸릴수록 실제보다 작은 값이 백엔드에 적재된다.
    """
    _install(monkeypatch, _FakeMessages(result=_response(usage=_usage(100, 20, 30, 40))))

    usage = (await anthropic_sdk.complete(_req(), _THINKING)).usage

    assert usage.input_tokens == 170  # 100 + 30 + 40
    assert usage.output_tokens == 20
    # 내역도 보존한다(진단용).
    assert usage.cache_creation_input_tokens == 30
    assert usage.cache_read_input_tokens == 40


async def test_usage_without_cache_parts_uses_the_plain_count(monkeypatch) -> None:
    """캐시가 안 걸린 호출은 캐시 칸이 비어 온다 — 그때는 일반 입력만이 전체 입력이다."""
    _install(monkeypatch, _FakeMessages(result=_response(usage=Usage(input_tokens=7, output_tokens=3))))

    usage = (await anthropic_sdk.complete(_req(), _THINKING)).usage

    assert usage.input_tokens == 7
    assert usage.cache_creation_input_tokens is None


async def test_boolean_token_counts_are_rejected(monkeypatch) -> None:
    """참/거짓은 토큰 수가 아니다 — None으로 떨어뜨린다(KNK-676 리뷰 P3).

    파이썬에서 `True`는 정수의 하위 타입이라 `isinstance(값, int)` 검사를 그냥 통과한다.
    그대로 두면 백엔드에 숫자 대신 JSON `true`가 실려 나가고(계약은 `int | null`),
    합산에서는 `True`가 1로 세어져 토큰 수가 조용히 틀린다.
    """
    broken_usage = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=True,
            output_tokens=True,
            cache_creation_input_tokens=False,
            cache_read_input_tokens=7,
        )
    )
    _install(
        monkeypatch,
        _FakeMessages(
            result=SimpleNamespace(
                content=[], model="anthropic-test-model", stop_reason=None, **vars(broken_usage)
            )
        ),
    )

    usage = (await anthropic_sdk.complete(_req(), _THINKING)).usage

    # 참/거짓은 빠지고 진짜 숫자만 남는다 — 1로 세어지지 않는다.
    assert usage.input_tokens == 7
    assert usage.output_tokens is None
    assert usage.cache_creation_input_tokens is None
    assert usage.cache_read_input_tokens == 7


async def test_usage_missing_stays_none(monkeypatch) -> None:
    """usage 자체가 없으면 0이 아니라 None으로 남긴다(백엔드 계약: 누락 시 null).

    정상 응답에는 usage가 반드시 있으므로 이 경로는 껍데기가 깨진 응답 대비다 — 그래서
    여기서만 실제 SDK 타입 대신 손으로 만든 응답을 쓴다.
    """
    broken = SimpleNamespace(content=[], model="anthropic-test-model", usage=None, stop_reason=None)
    _install(monkeypatch, _FakeMessages(result=broken))

    usage = (await anthropic_sdk.complete(_req(), _THINKING)).usage

    assert usage.input_tokens is None
    assert usage.output_tokens is None


# ── 본문 ─────────────────────────────────────────────────────────────────────
async def test_text_joins_only_text_blocks(monkeypatch) -> None:
    """본문은 블록 목록으로 온다 — 글 블록만 골라 잇고 추론 블록은 버린다.

    추론 블록을 그대로 이으면 모델의 속생각이 사용자 화면에 그대로 나간다.
    """
    _install(
        monkeypatch,
        _FakeMessages(
            result=_response(
                content=[
                    ThinkingBlock(type="thinking", thinking="속생각", signature="sig"),
                    TextBlock(type="text", text="안"),
                    TextBlock(type="text", text="녕"),
                ]
            )
        ),
    )

    result = await anthropic_sdk.complete(_req(), _THINKING)

    assert result.text == "안녕"


async def test_text_ignores_blocks_that_are_not_text_blocks(monkeypatch) -> None:
    """글자를 담고 있어도 **종류가 글 블록이 아니면** 본문에 넣지 않는다.

    윗 테스트(추론 블록)만으로는 이 규칙이 고정되지 않는다 — 추론 블록은 글을 `thinking`에
    담아서, 종류를 안 가리고 "글자 필드가 있나"만 봐도 어차피 걸러진다(변이 시험으로 확인).
    그래서 **글자를 `text`에 담으면서 종류가 다른** 실제 SDK 타입으로 확인한다.

    `TextDelta`는 스트리밍 조각이라 단발 응답 본문에 올 일이 없다. 여기서는 "그런 모양의
    객체"를 실제 타입으로 세우려고 빌려 쓴다 — 손으로 만들면 진짜와 달라질 위험이 있다.
    응답도 검증을 건너뛰고 만든다(정상 타입이면 애초에 담기지 않는 모양이라서다).
    """
    response = AnthropicMessage.model_construct(
        id="msg_1",
        content=[
            AnthropicTextDelta(type="text_delta", text="조각"),
            TextBlock(type="text", text="본문"),
        ],
        model="anthropic-test-model",
        role="assistant",
        type="message",
        stop_reason="end_turn",
        usage=_usage(),
    )
    _install(monkeypatch, _FakeMessages(result=response))

    result = await anthropic_sdk.complete(_req(), _THINKING)

    assert result.text == "본문"


async def test_complete_maps_result_fields(monkeypatch) -> None:
    _install(monkeypatch, _FakeMessages(result=_response(stop_reason="max_tokens")))

    result = await anthropic_sdk.complete(_req(), _THINKING)

    assert result.text == "본문"
    assert result.model == "anthropic-test-model"
    assert result.provider == PROVIDER_ANTHROPIC
    # 값의 어휘가 회사마다 다르다 — 길이 상한에 걸렸을 때 OpenAI 계열은 "length"다.
    assert result.finish_reason == "max_tokens"


async def test_result_provider_follows_the_model(monkeypatch) -> None:
    """결과의 provider는 그 모델의 공급자다 — 상수를 박아도 통과하면 안 된다."""
    other = ResolvedModel(
        model="anthropic-test-model",
        provider="not-anthropic",
        adapter=ADAPTER_ANTHROPIC_SDK,
        use_thinking=True,
    )
    _install(monkeypatch, _FakeMessages(result=_response()))

    result = await anthropic_sdk.complete(_req(), other)

    assert result.provider == "not-anthropic"


@pytest.mark.parametrize(
    "broken",
    [
        # 목록 자리에 목록이 아닌 것 / 글 자리에 글자가 아닌 것 — 모양을 나열해 막지 않고
        # "못 꺼내면 빈 글" 한 규칙으로 덮는다(경우가 끝이 없다).
        SimpleNamespace(content=None, model="m", usage=None, stop_reason=None),
        SimpleNamespace(content=1, model="m", usage=None, stop_reason=None),
        SimpleNamespace(content=[], model="m", usage=None, stop_reason=None),
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text=None)],
            model="m",
            usage=None,
            stop_reason=None,
        ),
        SimpleNamespace(content=[{"type": "text", "text": "본문"}], model="m", usage=None, stop_reason=None),
    ],
)
async def test_broken_response_returns_empty_text(monkeypatch, broken) -> None:
    """응답 껍데기가 깨져도 예외를 던지지 않는다 — 빈 글로 넘긴다.

    여기서 예외를 던지면 스토리라인 invalid 재호출(KNK-312)이 전송 오류 경로로 새서 사라진다.
    """
    _install(monkeypatch, _FakeMessages(result=broken))

    result = await anthropic_sdk.complete(_req(), _THINKING)

    assert result.text == ""


async def test_missing_stop_reason_is_none_not_an_error(monkeypatch) -> None:
    """종료 이유를 못 꺼내도 예외가 아니라 None이다.

    위 "본문" 규칙과 같다 — 응답 껍데기가 깨진 것을 공급자 장애로 둔갑시키지 않는다.
    이 경로는 `stop_reason` 칸이 **아예 없는** 응답에서만 밟히므로 따로 세운다.
    """
    without_stop_reason = SimpleNamespace(content=[], model="anthropic-test-model", usage=None)
    _install(monkeypatch, _FakeMessages(result=without_stop_reason))

    result = await anthropic_sdk.complete(_req(), _THINKING)

    assert result.finish_reason is None


async def test_model_falls_back_to_requested_name(monkeypatch) -> None:
    """응답에 모델명이 비어 오면 요청에 쓴 이름으로 채운다(빈 값은 meta 조립에서 터진다)."""
    broken = SimpleNamespace(content=[], model=None, usage=None, stop_reason=None)
    _install(monkeypatch, _FakeMessages(result=broken))

    result = await anthropic_sdk.complete(_req(model="anthropic-test-model"), _THINKING)

    assert result.model == "anthropic-test-model"


# ── 예외 번역 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("timeout", LlmTimeout),
        ("rate", LlmRateLimited),
        ("bad", LlmBadRequest),
        ("connection", LlmUnavailable),
        # 4xx라고 다 bad_request가 아니다 — 400만 요청 오류이고 나머지는 일시 장애로 묶는다
        # (openai_sdk와 같은 경계여야 error_code와 502 문구가 공급자에 따라 달라지지 않는다).
        ("auth", LlmUnavailable),
        ("forbidden", LlmUnavailable),
        ("not_found", LlmUnavailable),
        ("unprocessable", LlmUnavailable),
        ("server", LlmUnavailable),
    ],
)
async def test_translates_sdk_errors(monkeypatch, kind: str, expected: type) -> None:
    _install(monkeypatch, _FakeMessages(error=_anthropic_error(kind)))

    with pytest.raises(expected) as exc_info:
        await anthropic_sdk.complete(_req(), _THINKING)

    # 실패 경로엔 결과가 없으므로 예외가 provider·model의 유일한 출처다.
    assert exc_info.value.provider == PROVIDER_ANTHROPIC
    assert exc_info.value.model == "anthropic-test-model"


async def test_timeout_is_checked_before_connection_error(monkeypatch) -> None:
    """이 SDK도 APITimeoutError가 APIConnectionError의 하위다 — 순서가 뒤집히면 타임아웃이 사라진다.

    (0.120.0에서 직접 확인. openai SDK와 같은 함정이다.)
    """
    from anthropic import APIConnectionError, APITimeoutError

    assert issubclass(APITimeoutError, APIConnectionError)
    _install(monkeypatch, _FakeMessages(error=_anthropic_error("timeout")))

    with pytest.raises(LlmTimeout):
        await anthropic_sdk.complete(_req(), _THINKING)


# ── 스트리밍 (KNK-696) ───────────────────────────────────────────────────────
# 이벤트도 손으로 만들지 않고 SDK 실제 타입으로 만든다. 이 회사는 신호 종류가 여럿이라
# (시작·조각·끝) 손으로 흉내 내면 어느 신호에 무엇이 실리는지를 우리가 정하게 되고,
# 그러면 테스트가 실제 계약이 아니라 우리 가정을 증명하게 된다.
def _start_event(model="anthropic-test-model", usage=None):
    """시작 신호 — 모델 이름과 **입력** 토큰이 여기 실린다. 본문은 아직 비어 있다."""
    return RawMessageStartEvent(
        type="message_start",
        message=_response(
            content=[],
            model=model,
            # 시작 시점의 출력 토큰은 실제 총량이 아니다(보통 1~2). 끝 신호가 덮어써야 한다.
            usage=usage if usage is not None else _usage(output_tokens=1),
            stop_reason=None,
        ),
    )


def _text_event(text: str):
    return RawContentBlockDeltaEvent(
        type="content_block_delta", index=0, delta=AnthropicTextDelta(type="text_delta", text=text)
    )


def _thinking_event(thinking: str = "속으로 생각한 말"):
    return RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=0,
        delta=ThinkingDelta(type="thinking_delta", thinking=thinking),
    )


def _stop_event(stop_reason="end_turn", **usage_fields):
    """끝 신호 — 종료 이유와 **최종 출력** 토큰이 여기 실린다.

    usage의 입력 칸들은 기본이 None이다(SDK 0.120.0). 그래서 이 신호만 읽으면 입력 토큰이
    통째로 빈다 — `test_stream_merges_tokens_from_two_events`가 그것을 고정한다.
    """
    return RawMessageDeltaEvent(
        type="message_delta",
        delta=AnthropicStopDelta(stop_reason=stop_reason, stop_sequence=None),
        usage=MessageDeltaUsage(**({"output_tokens": 20} | usage_fields)),
    )


class _FakeStream:
    """실제 `anthropic.AsyncStream`의 모양을 흉내 낸다 — 반복 가능하고 `close()`로 닫힌다.

    async generator로 대신하면 `close()`가 없어(그쪽은 `aclose()`) 어댑터가 스트림을 닫는지
    검증할 수 없다. 목이 실제 타입과 다르면 통과해도 아무것도 증명하지 못한다.
    """

    def __init__(self, events: list, error: BaseException | None = None) -> None:
        self._events = events
        self._error = error
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self.closed = True


def _stream_of(events: list, error: BaseException | None = None) -> _FakeStream:
    return _FakeStream(events, error)


async def test_stream_yields_deltas_then_completed(monkeypatch) -> None:
    events_in = [_start_event(), _text_event("안"), _text_event("녕"), _stop_event()]
    _install(monkeypatch, _FakeMessages(result=_stream_of(events_in)))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[:2] == [TextDelta("안"), TextDelta("녕")]
    # 이벤트 구성 전체를 고정한다 — 마지막 것만 보면 종료 이벤트가 두 번 나가도 통과한다.
    assert [type(ev) for ev in events] == [TextDelta, TextDelta, StreamCompleted]
    assert sum(isinstance(ev, StreamCompleted) for ev in events) == 1
    assert events[-1].model == "anthropic-test-model"
    assert events[-1].provider == PROVIDER_ANTHROPIC
    assert events[-1].finish_reason == "end_turn"


async def test_stream_merges_tokens_from_two_events(monkeypatch) -> None:
    """**입력은 시작 신호, 최종 출력은 끝 신호에 온다 — 둘을 합쳐야 맞다.**

    이 어댑터에서 제일 틀리기 쉬운 곳이다. 한쪽만 읽어도 오류가 나지 않고, 백엔드에 절반이
    빈 값이 적재될 뿐이라 조용히 틀린다.

    - 끝 신호만 읽으면 → 입력 토큰이 전부 None
    - 시작 신호만 읽으면 → 출력 토큰이 1(시작 시점의 값)로 굳는다
    - 끝 신호로 통째로 덮어쓰면 → 입력 토큰이 마지막에 지워진다
    """
    _install(monkeypatch, _FakeMessages(result=_stream_of([_start_event(), _stop_event()])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    usage = events[-1].usage
    assert usage.input_tokens == 170  # 100(일반) + 30(캐시 생성) + 40(캐시 읽기)
    assert usage.output_tokens == 20  # 시작 신호의 1이 아니다
    assert usage.cache_creation_input_tokens == 30
    assert usage.cache_read_input_tokens == 40


async def test_stream_takes_the_later_input_tokens_when_both_carry_them(monkeypatch) -> None:
    """끝 신호가 입력 토큰을 다시 보내면 그쪽을 쓴다 — 두 값 다 '지금까지의 총합'이라서다.

    (더하면 두 배가 된다. `_absorb_usage`가 더하지 않고 덮어쓰는 이유가 이것이다.)
    """
    _install(
        monkeypatch,
        _FakeMessages(
            result=_stream_of([_start_event(), _stop_event(input_tokens=111, output_tokens=20)])
        ),
    )

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[-1].usage.input_tokens == 111 + 30 + 40


async def test_stream_keeps_absorbing_after_the_first_finish_signal(monkeypatch) -> None:
    """마무리 신호는 **여러 번** 올 수 있다 — 첫 개만 읽고 멈추면 최종값을 놓친다.

    실제 계약이 그렇다(SDK 자체 누적 코드도 매번 갱신한다). 값은 매번 '지금까지의 총합'이라
    마지막 것이 맞다. 앞 테스트들은 신호를 하나만 보내서 "첫 개 뒤로는 안 읽는" 변이를
    놓친다(KNK-696 리뷰 P2).
    """
    first = _stop_event(stop_reason=None, output_tokens=5)
    last = _stop_event(stop_reason="max_tokens", output_tokens=42, cache_read_input_tokens=99)
    _install(monkeypatch, _FakeMessages(result=_stream_of([_start_event(), first, last])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[-1].usage.output_tokens == 42
    assert events[-1].usage.cache_read_input_tokens == 99
    assert events[-1].usage.input_tokens == 100 + 30 + 99  # 캐시 읽기가 나중 값으로 갱신됐다
    assert events[-1].finish_reason == "max_tokens"


async def test_stream_usage_stays_none_without_any_signal(monkeypatch) -> None:
    """토큰 신호가 하나도 없으면 0이 아니라 None이다(백엔드 계약: 누락 시 null).

    마무리 신호는 오되 그 안에 토큰이 없는 모양이라 손으로 만든다 — SDK의 진짜 타입은
    출력 토큰을 필수로 받아 "토큰 없는 마무리"를 표현할 수 없다.
    """
    no_usage = SimpleNamespace(
        type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=None
    )
    _install(monkeypatch, _FakeMessages(result=_stream_of([_text_event("가"), no_usage])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[-1].usage == TokenUsage()


async def test_stream_drops_thinking_deltas(monkeypatch) -> None:
    """추론 조각은 흘리지 않는다 — 그대로 내보내면 모델의 생각이 사용자 화면에 그대로 뜬다."""
    events_in = [_start_event(), _thinking_event(), _text_event("안"), _stop_event()]
    _install(monkeypatch, _FakeMessages(result=_stream_of(events_in)))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert [ev for ev in events if isinstance(ev, TextDelta)] == [TextDelta("안")]


@pytest.mark.parametrize("weird", [None, 123, ["안녕"], {"text": "안녕"}, ""])
async def test_stream_drops_non_text_delta(monkeypatch, weird: object) -> None:
    """글자가 아닌 조각은 흘리지 않는다 — 화면에 그 표기가 뜨거나 이어붙이다 터진다.

    깨진 모양은 SimpleNamespace로 만든다. 정의상 정상 SDK 타입이 아니라서 진짜 타입으로는
    만들 수 없다(만들려 하면 SDK가 먼저 거부한다).
    """
    broken = SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=weird)
    )
    events_in = [_start_event(), _text_event("안"), broken, _text_event("녕"), _stop_event()]
    _install(monkeypatch, _FakeMessages(result=_stream_of(events_in)))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert [ev for ev in events if isinstance(ev, TextDelta)] == [TextDelta("안"), TextDelta("녕")]
    assert events[-1].finish_reason == "end_turn"  # 뒤 신호도 계속 읽었다


async def test_stream_ignores_other_event_kinds(monkeypatch) -> None:
    """읽을 것이 없는 신호는 조용히 지나간다 — 여기서 터지면 정상 응답이 오류가 된다."""
    events_in = [
        _start_event(),
        RawContentBlockStartEvent(
            type="content_block_start", index=0, content_block=TextBlock(type="text", text="")
        ),
        _text_event("가"),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        _stop_event(),
        RawMessageStopEvent(type="message_stop"),
    ]
    _install(monkeypatch, _FakeMessages(result=_stream_of(events_in)))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert [type(ev) for ev in events] == [TextDelta, StreamCompleted]


async def test_stream_keeps_the_providers_own_stop_word(monkeypatch) -> None:
    """종료 이유를 우리 어휘로 바꾸지 않고 그대로 올린다.

    길이 상한에 걸렸을 때 이 회사는 `max_tokens`, OpenAI 계열은 `length`다. 지금 이 값을
    읽어 판정하는 코드가 없어 문제되지 않지만, 잘림 감지를 붙이는 날 두 어휘를 함께 봐야 한다.
    """
    events_in = [_start_event(), _text_event("가"), _stop_event(stop_reason="max_tokens")]
    _install(monkeypatch, _FakeMessages(result=_stream_of(events_in)))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[-1].finish_reason == "max_tokens"


async def test_stream_ignores_a_non_text_stop_reason(monkeypatch) -> None:
    """종료 이유도 글자일 때만 쓴다 — 이상한 값이 응답 meta에 그대로 실려 나가지 않게."""
    broken = SimpleNamespace(
        type="message_delta", delta=SimpleNamespace(stop_reason=["end_turn"]), usage=None
    )
    _install(monkeypatch, _FakeMessages(result=_stream_of([_start_event(), broken])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[-1].finish_reason is None


async def test_stream_drops_a_delta_that_only_looks_like_text(monkeypatch) -> None:
    """조각 종류를 **이름으로** 거른다 — `text` 칸이 있는지로 거르면 안 된다.

    지금 이 회사의 조각 종류 중 글이 아닌 것들은 글을 `text`가 아닌 칸(`thinking`·
    `signature`)에 담아, 종류 검사를 빼도 우연히 걸러진다. 그래서 검사를 지워도 다른
    테스트는 전부 통과한다 — 종류가 하나 늘어나는 날 그 조각이 그대로 사용자 화면에 뜬다.
    여기서 "글 칸을 가진 다른 종류"를 만들어 그 우연에 기대지 않게 한다.
    """
    disguised = SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="thinking_delta", text="속으로 생각한 말"),
    )
    _install(monkeypatch, _FakeMessages(result=_stream_of([disguised, _text_event("안"), _stop_event()])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert [ev for ev in events if isinstance(ev, TextDelta)] == [TextDelta("안")]


@pytest.mark.parametrize(
    "leading",
    [
        [],  # 시작 신호가 아예 없다
        [SimpleNamespace(type="message_start", message=SimpleNamespace(model=None, usage=None))],
    ],
)
async def test_stream_model_falls_back_to_the_requested_name(monkeypatch, leading: list) -> None:
    """시작 신호가 없거나 모델명이 비어도 요청에 쓴 이름으로 채운다(빈 값은 meta 조립에서 터진다)."""
    _install(
        monkeypatch, _FakeMessages(result=_stream_of([*leading, _text_event("가"), _stop_event()]))
    )

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert events[-1].model == "anthropic-test-model"


async def test_stream_completed_provider_follows_the_model(monkeypatch) -> None:
    """종료 신호의 provider는 그 모델의 공급자다 — 상수를 박아도 통과하면 안 된다."""
    other = ResolvedModel(
        model="anthropic-test-model",
        provider="not-anthropic",
        adapter=ADAPTER_ANTHROPIC_SDK,
        use_thinking=True,
    )
    _install(monkeypatch, _FakeMessages(result=_stream_of([_start_event(), _stop_event()])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), other)]

    assert events[-1].provider == "not-anthropic"


async def test_stream_sends_exact_kwargs(monkeypatch) -> None:
    """스트리밍도 단발 호출과 **같은 조립**을 거친다 — 조각 흘리기 표시만 더 붙는다.

    dict 전체를 비교한다. `stream=True`만 확인하면 이 경로가 조립을 건너뛰어도 통과하는데,
    그러면 지시문(system)이 대화 목록 안에 남은 채로 나간다. 이 회사는 대화 줄의 역할로
    system을 받지 않아 **채팅 턴마다 400**이 된다 — 그리고 스트리밍을 쓰는 호출부는 채팅뿐이라
    운영에서 채팅만 통째로 죽는다(이 구멍을 실제 변이로 확인하고 이 테스트를 세웠다).
    """
    messages = _FakeMessages(result=_stream_of([_text_event("가"), _stop_event()]))
    _install(monkeypatch, messages)

    [ev async for ev in anthropic_sdk.stream(_req(temperature=0.75, timeout=88.5), _NO_THINKING)]

    assert messages.captured == {
        "model": "anthropic-test-model",
        "messages": [{"role": "user", "content": "u"}],  # system이 빠져나갔다
        "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
        "max_tokens": 16,
        "thinking": {"type": "disabled"},
        "temperature": 0.75,
        "timeout": 88.5,
        "stream": True,
    }


async def test_stream_does_not_require_max_tokens(monkeypatch) -> None:
    """스트리밍에는 max_tokens 가드가 없다 — 우리 쪽에서 막지 않고 그대로 보낸다.

    가드를 `_build_kwargs`가 아니라 `complete`에 둔 이유가 이것이다. 조립부에 두면 이 경로도
    함께 막힌다. 막지 않는 판단의 근거는 `stream`의 설명 참조(공급자가 거부하면 그쪽 400이
    LlmBadRequest로 접혀 SSE 오류 이벤트가 정상으로 나간다 — 우리가 막으면 그러지 못한다).
    """
    messages = _FakeMessages(result=_stream_of([_text_event("가"), _stop_event()]))
    _install(monkeypatch, messages)

    events = [ev async for ev in anthropic_sdk.stream(_req(max_tokens=None), _THINKING)]

    assert events[0] == TextDelta("가")
    assert messages.captured["max_tokens"] is NOT_GIVEN  # 요청 본문에서 빠진다


async def test_stream_error_after_deltas_becomes_neutral_error(monkeypatch) -> None:
    """일부를 흘려보낸 뒤 실패해도 중립 예외로 바뀐다 — 호출부가 SSE error로 옮긴다.

    스트림 도중의 `error` 신호도 이 경로다 — SDK가 그 신호를 자기 예외로 바꿔 올린다.
    """
    _install(
        monkeypatch,
        _FakeMessages(result=_stream_of([_text_event("가")], error=_anthropic_error("rate"))),
    )

    seen: list = []
    with pytest.raises(LlmRateLimited):
        async for ev in anthropic_sdk.stream(_req(), _THINKING):
            seen.append(ev)

    assert seen == [TextDelta("가")]  # 이미 보낸 조각은 그대로 나갔다


async def test_stream_failure_before_the_first_event(monkeypatch) -> None:
    """조각이 하나도 오기 전에 실패해도 중립 예외다.

    async generator라 부른 자리가 아니라 **첫 조각을 받으려는 순간** 드러난다.
    """
    _install(monkeypatch, _FakeMessages(error=_anthropic_error("timeout")))
    events = anthropic_sdk.stream(_req(), _THINKING)  # 여기서는 아직 안 터진다

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

    이 SDK의 스트림 반복도 try/finally로만 감싸져(except 없음) 연결 끊김·깨진 SSE 줄이 번역
    없이 통과한다. 그대로 두면 채팅은 error 이벤트도 못 내고 끊긴다.
    """
    _install(monkeypatch, _FakeMessages(result=_stream_of([_text_event("가")], error=escaping)))

    with pytest.raises(LlmUnavailable) as exc_info:
        async for _ in anthropic_sdk.stream(_req(), _THINKING):
            pass

    assert exc_info.value.provider == PROVIDER_ANTHROPIC
    assert type(escaping).__name__ in str(exc_info.value)


async def test_stream_read_timeout_stays_a_timeout(monkeypatch) -> None:
    """스트림이 열린 뒤의 읽기 시간 초과도 '시간 초과'다.

    접는 자리를 나누지 않으면 같은 시간 초과가 발생 시점에 따라 다른 코드·다른 사용자 문구가 된다.
    """
    _install(
        monkeypatch,
        _FakeMessages(
            result=_stream_of([_text_event("가")], error=httpx.ReadTimeout("응답이 멈췄습니다"))
        ),
    )

    with pytest.raises(LlmTimeout):
        async for _ in anthropic_sdk.stream(_req(), _THINKING):
            pass


async def test_stream_cut_short_is_an_error_not_a_quiet_completion(monkeypatch) -> None:
    """마무리 신호 없이 스트림이 끝나면 오류다 — 잘린 답을 완성된 답으로 넘기지 않는다.

    **SDK는 이 상황에 예외를 내지 않는다**(실제 SDK + 가짜 전송으로 재현). 중간 프록시가
    본문을 자르면서 연결을 정상 종료로 닫으면 반복이 그냥 끝난다. 막지 않으면 잘린 본문이
    채팅 답으로 저장되고, 출력 토큰도 시작 시점 값(보통 1)으로 굳는다(KNK-696 리뷰 P1).
    """
    _install(monkeypatch, _FakeMessages(result=_stream_of([_start_event(), _text_event("잘린")])))

    seen: list = []
    with pytest.raises(LlmUnavailable) as exc_info:
        async for ev in anthropic_sdk.stream(_req(), _THINKING):
            seen.append(ev)

    assert seen == [TextDelta("잘린")]  # 이미 보낸 조각은 그대로 나갔다
    assert not any(isinstance(ev, StreamCompleted) for ev in seen)
    assert exc_info.value.provider == PROVIDER_ANTHROPIC


async def test_stream_accepts_an_unreadable_stop_reason_as_finished(monkeypatch) -> None:
    """"끝났다"는 신호는 왔는데 그 이유를 알아볼 수 없는 경우는 오류가 아니다.

    윗 테스트와 짝이다. 둘을 구분하지 않고 "종료 이유 값"으로만 판정하면, 값이 이상하게 온
    정상 응답까지 전송 오류로 둔갑한다.
    """
    unreadable = SimpleNamespace(
        type="message_delta", delta=SimpleNamespace(stop_reason=["end_turn"]), usage=None
    )
    _install(monkeypatch, _FakeMessages(result=_stream_of([_text_event("가"), unreadable])))

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert isinstance(events[-1], StreamCompleted)
    assert events[-1].finish_reason is None


async def test_stream_broken_bytes_become_a_neutral_error(monkeypatch) -> None:
    """SSE 원문 바이트가 깨져도 중립 예외로 접는다.

    이 SDK는 원문을 자기가 UTF-8로 푸는데, 깨진 바이트에서 나는 `UnicodeDecodeError`는
    SDK 예외도 httpx 오류도 아니라 접는 목록에 따로 넣어야 한다. 빠뜨리면 채팅이 오류
    이벤트도 못 내고 끊긴다(KNK-696 리뷰 P1, 실제 SDK로 재현 확인).
    """
    broken_bytes = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    _install(monkeypatch, _FakeMessages(result=_stream_of([_text_event("가")], error=broken_bytes)))

    with pytest.raises(LlmUnavailable) as exc_info:
        async for _ in anthropic_sdk.stream(_req(), _THINKING):
            pass

    assert "UnicodeDecodeError" in str(exc_info.value)


async def test_stream_cancellation_is_not_an_error(monkeypatch) -> None:
    """사용자 연결 취소는 오류가 아니다 — 중립 예외로 바꾸지 않고 그대로 통과시킨다."""
    _install(
        monkeypatch,
        _FakeMessages(result=_stream_of([_text_event("가")], error=asyncio.CancelledError())),
    )

    with pytest.raises(asyncio.CancelledError):
        async for _ in anthropic_sdk.stream(_req(), _THINKING):
            pass


async def test_stream_is_closed_after_normal_finish(monkeypatch) -> None:
    fake_stream = _stream_of([_text_event("가"), _stop_event()])
    _install(monkeypatch, _FakeMessages(result=fake_stream))

    [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert fake_stream.closed is True


async def test_stream_is_closed_when_consumer_leaves_early(monkeypatch) -> None:
    """사용자가 중간에 떠나면 스트림을 닫아 커넥션을 반납한다.

    `break`만으로는 부족하다 — 파이썬은 제너레이터를 그 자리에서 정리하지 않고 나중에 회수한다.
    실제 SSE 경로는 연결이 끊기면 태스크가 취소되면서 제너레이터가 닫히므로, 그 정리 시점을
    여기서 재현한다.
    """
    fake_stream = _stream_of([_text_event("가"), _text_event("나")])
    _install(monkeypatch, _FakeMessages(result=fake_stream))
    events = anthropic_sdk.stream(_req(), _THINKING)

    async for _ in events:
        break  # 첫 조각만 받고 떠난다
    await events.aclose()  # 취소·회수 시점

    assert fake_stream.closed is True


async def test_close_failure_does_not_mask_the_real_error(monkeypatch) -> None:
    """정리하다 실패해도 원래 오류가 그대로 올라온다 — 정리 예외가 원인을 덮으면 안 된다."""

    class _UncloseableStream(_FakeStream):
        async def close(self) -> None:
            raise RuntimeError("정리 실패")

    _install(
        monkeypatch,
        _FakeMessages(
            result=_UncloseableStream([_text_event("가")], error=_anthropic_error("rate"))
        ),
    )

    with pytest.raises(LlmRateLimited):  # RuntimeError가 아니라 원래 오류
        async for _ in anthropic_sdk.stream(_req(), _THINKING):
            pass


async def test_close_failure_does_not_break_normal_finish(monkeypatch) -> None:
    """정상 종료도 마찬가지다 — 정리 실패로 완료 이벤트가 사라지면 안 된다."""

    class _UncloseableStream(_FakeStream):
        async def close(self) -> None:
            raise RuntimeError("정리 실패")

    _install(
        monkeypatch, _FakeMessages(result=_UncloseableStream([_text_event("가"), _stop_event()]))
    )

    events = [ev async for ev in anthropic_sdk.stream(_req(), _THINKING)]

    assert isinstance(events[-1], StreamCompleted)


# ── 통로 연결 ────────────────────────────────────────────────────────────────
async def test_gateway_routes_to_the_anthropic_adapter(monkeypatch) -> None:
    """통로가 이 어댑터를 실제로 고른다 — 어댑터만 만들고 분기를 안 넣으면 아무도 못 쓴다."""
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    messages = _FakeMessages(result=_response())
    _install(monkeypatch, messages)

    result = await llm.complete(_req())

    assert result.provider == PROVIDER_ANTHROPIC
    assert messages.captured is not None  # 통로가 다른 어댑터로 새지 않았다


async def test_gateway_stream_routes_to_the_anthropic_adapter(monkeypatch) -> None:
    """통로의 스트리밍도 이 어댑터로 간다 — complete만 배선하고 stream을 빠뜨리면 여기서 걸린다."""
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    messages = _FakeMessages(result=_stream_of([_start_event(), _text_event("가"), _stop_event()]))
    _install(monkeypatch, messages)

    events = [ev async for ev in llm.stream(_req())]

    assert events[0] == TextDelta("가")
    assert isinstance(events[-1], StreamCompleted)
    assert events[-1].provider == PROVIDER_ANTHROPIC


def test_startup_check_accepts_both_thinking_settings() -> None:
    """기동 검사는 추론 켬·끔 둘 다 통과시킨다 — 이 회사 문법이 둘 다 표현할 수 있다."""
    anthropic_sdk.check_supported(_THINKING)
    anthropic_sdk.check_supported(_NO_THINKING)


# ── 스트리밍 못 하는 어댑터를 스트리밍 자리에 못 꽂게 한다 (KNK-676 리뷰 P2) ──
def test_adapters_declare_whether_they_can_stream() -> None:
    """어댑터는 자기가 조각 흘리기를 할 수 있는지 밝힌다 — 기동 검사가 이 값을 읽는다.

    **기본값을 두지 않는 것이 중요하다.** 새 어댑터가 이 값을 빠뜨리면 기동 검사가
    AttributeError로 즉시 드러낸다. 기본값이 True면 못 하는 어댑터가 조용히 통과한다.

    지금은 둘 다 할 수 있다(KNK-696). 그래서 **아래 두 테스트는 값을 일부러 False로 바꿔
    확인한다** — 못 하는 어댑터가 다시 생길 때를 위해 막는 장치 자체를 살려두는 것이다.
    """
    assert anthropic_sdk.SUPPORTS_STREAMING is True
    assert openai_sdk.SUPPORTS_STREAMING is True


def test_startup_rejects_a_non_streaming_adapter_in_a_streaming_slot(monkeypatch) -> None:
    """조각 흘리기를 못 하는 어댑터를 CHAT_MODEL에 꽂으면 **기동에서** 막는다.

    막지 않으면 서버는 아무 오류 없이 뜨고, 사용자가 채팅을 눌러야 터진다. 그것도 조용히
    터진다 — 없는 `stream`이 던지는 것은 채팅이 잡는 예외 계열(LlmError)이 아니라 SSE 오류
    이벤트도 못 내고 스트림이 끊긴다(재현 확인).

    메시지에 어느 env를 되돌릴지도 있어야 한다 — 배포가 실패해 서버가 내려간 상황이다.

    **자리별 금지 공급자 검사를 일부러 꺼두고 본다.** 그 검사가 먼저 걸러버리면 여기서 확인하려는
    그물(스트리밍 능력)이 한 번도 실행되지 않는데, 통과하는 모습은 똑같아 눈치채기 어렵다.
    """
    monkeypatch.setattr(registry, "BLOCKED_PROVIDERS", {})
    monkeypatch.setattr(anthropic_sdk, "SUPPORTS_STREAMING", False)
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    monkeypatch.setattr(
        registry,
        "settings",
        Settings(
            _env_file=None,
            deepseek_api_key="k",
            anthropic_api_key="ant",
            chat_model="anthropic-test-model",
        ),
    )

    with pytest.raises(LlmConfigError) as exc_info:
        llm.validate_startup()

    message = str(exc_info.value)
    assert "CHAT_MODEL" in message
    assert "스트리밍" in message


def test_startup_allows_a_non_streaming_adapter_in_a_non_streaming_slot(monkeypatch) -> None:
    """같은 어댑터라도 스트리밍을 안 쓰는 자리면 통과한다.

    **짝으로 확인해야 의미가 있다.** 윗 테스트만 보면 "이 어댑터를 아예 못 쓰게 막는" 코드도
    똑같이 통과한다 — 막은 것이 "이 어댑터"가 아니라 "스트리밍 자리"임을 여기서 고정한다.
    """
    monkeypatch.setattr(anthropic_sdk, "SUPPORTS_STREAMING", False)
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    monkeypatch.setattr(
        registry,
        "settings",
        Settings(
            _env_file=None,
            deepseek_api_key="k",
            anthropic_api_key="ant",
            storylines_model="anthropic-test-model",
        ),
    )

    llm.validate_startup()  # 예외가 나면 실패다


def test_the_streaming_slot_is_known_only_to_the_registry() -> None:
    """용도 이름은 등록부에만 있다 — 통로도 어댑터도 "채팅"을 몰라야 한다.

    용도에 맞춰 아래층을 깎으면 모델·공급자를 바꿀 때마다 그 층을 다시 고쳐야 한다(KNK-667).
    """
    assert registry.STREAMING_ENVS == frozenset({"CHAT_MODEL"})

    gateway_source = Path(llm.__file__).read_text(encoding="utf-8")
    adapter_source = Path(anthropic_sdk.__file__).read_text(encoding="utf-8")
    # 통로 docstring은 세 env를 "무엇을 고칠지" 안내로 나열한다 — 코드에 박혔는지를 본다.
    assert "CHAT_MODEL" not in _code_of(gateway_source)
    assert "CHAT_MODEL" not in _code_of(adapter_source)


# ── 채팅 자리에 이 공급자를 못 꽂게 한다 (KNK-675) ──────────────────────────
def _blocked_settings(**overrides) -> Settings:
    values = {"_env_file": None, "deepseek_api_key": "k", "anthropic_api_key": "ant"}
    return Settings(**(values | overrides))


def test_the_blocked_slot_is_known_only_to_the_registry() -> None:
    """"채팅에는 이 공급자를 쓸 수 없다"는 규칙은 등록부에만 있다.

    `STREAMING_ENVS`와 같은 이유다 — 통로·어댑터에 용도 이름을 박으면 모델·공급자를 바꿀
    때마다 그 층을 다시 고쳐야 한다(KNK-667). 어느 파일에도 "CHAT_MODEL"이 코드로 박히지
    않았는지는 위 `test_the_streaming_slot_is_known_only_to_the_registry`가 함께 지킨다.
    """
    assert registry.BLOCKED_PROVIDERS == {"CHAT_MODEL": frozenset({PROVIDER_ANTHROPIC})}


def test_startup_rejects_this_provider_in_the_chat_slot(monkeypatch) -> None:
    """CHAT_MODEL에 이 공급자의 모델을 꽂으면 **기동에서** 막는다.

    막지 않으면 서버도 채팅도 정상으로 도는데 **안전 지시(PHI)만 빠진 채 호출된다** —
    이 회사는 지시문 칸이 하나뿐이라 채팅이 뒤에 두는 Depth·PHI가 버려지기 때문이다
    (`anthropic_sdk._split_system`). 오류가 나지 않아 경고 로그를 보지 않으면 모른다.

    직전까지는 "이 어댑터는 스트리밍을 못 한다"는 이유로 걸렸는데, 조각 흘리기를 구현하면서
    (KNK-696) 그 그물이 사라졌다. 이 테스트가 그 자리를 대신한다.
    """
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    monkeypatch.setattr(registry, "settings", _blocked_settings(chat_model="anthropic-test-model"))

    with pytest.raises(LlmConfigError) as exc_info:
        llm.validate_startup()

    message = str(exc_info.value)
    assert "CHAT_MODEL" in message  # 배포가 실패한 상황이다 — 무엇을 되돌릴지 있어야 한다
    assert PROVIDER_ANTHROPIC in message


def test_startup_allows_this_provider_in_other_slots(monkeypatch) -> None:
    """같은 모델이라도 채팅이 아닌 자리면 통과한다.

    **짝으로 확인해야 의미가 있다.** 윗 테스트만 보면 "이 공급자를 아예 못 쓰게 막는" 코드도
    똑같이 통과한다 — 막은 것이 "이 공급자"가 아니라 "채팅 자리"임을 여기서 고정한다.
    """
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    monkeypatch.setattr(
        registry, "settings", _blocked_settings(storylines_model="anthropic-test-model")
    )

    llm.validate_startup()  # 예외가 나면 실패다


def test_blocked_slot_is_reported_before_a_missing_key(monkeypatch) -> None:
    """못 쓰는 자리라는 사실을 키·주소보다 먼저 알린다.

    둘 다 어긋난 상태에서 "키가 비어 있습니다"라고 답하면, 키만 채우면 될 것처럼 읽혀
    엉뚱한 곳을 고치게 된다. 그리고 키를 채운 뒤에야 진짜 이유를 만난다.
    """
    monkeypatch.setitem(registry._REGISTRY, "anthropic-test-model", _THINKING)
    monkeypatch.setattr(
        registry,
        "settings",
        _blocked_settings(anthropic_api_key="", chat_model="anthropic-test-model"),
    )

    with pytest.raises(LlmConfigError) as exc_info:
        llm.validate_startup()

    assert "ANTHROPIC_API_KEY" not in str(exc_info.value)


# ── 클라이언트 생성·재사용 ────────────────────────────────────────────────────
# 위 테스트들은 전부 `_client`를 목으로 갈아끼운다 — 그래서 **이 절이 없으면 `_client`는 한 번도
# 실행되지 않는다.** 빈 키 가드도, 캐시 이름표도, 생성 인자도 전부 미검증으로 남는다.
@pytest.fixture
def _isolated_client_cache():
    """캐시를 비우고 테스트 후 원상복구한다 — 테스트가 서로의 클라이언트를 물려받지 않게."""
    saved = dict(anthropic_sdk._clients)
    anthropic_sdk._clients.clear()
    yield
    anthropic_sdk._clients.clear()
    anthropic_sdk._clients.update(saved)


def _creds(api_key: str = "test-key", base_url: str | None = None) -> ProviderCredentials:
    # base_url 기본값이 None인 것이 DeepSeek 쪽과 다르다 — 이 공급자는 자체 호스팅 주소가
    # 없는 게 보통이라 등록부가 None을 준다("SDK 기본 주소를 쓴다"는 뜻).
    return ProviderCredentials(
        api_key=api_key,
        base_url=base_url,
        api_key_env="ANTHROPIC_API_KEY",
        base_url_env="ANTHROPIC_API_URL",
    )


def test_client_is_built_with_declared_arguments(monkeypatch, _isolated_client_cache) -> None:
    """클라이언트 생성 인자(키·주소·재시도 횟수)를 단언한다.

    **재시도 횟수**가 이 테스트의 이유다. 숫자를 안 적고 SDK 기본값에 기대면 0으로 바뀌어도
    아무 테스트가 안 깨진다 — openai_sdk에서 실제로 그랬다(Codex 변이 시험). 그리고 두 어댑터의
    재시도 횟수가 어긋나면 공급자를 바꿨을 때 호출부의 시간 계산이 조용히 달라진다.
    """
    built: dict = {}

    class _SpyClient:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setattr(anthropic_sdk, "AsyncAnthropic", _SpyClient)
    monkeypatch.setattr(
        registry, "credentials", lambda provider: _creds(api_key="k", base_url="https://x.invalid")
    )

    anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert built == {"api_key": "k", "base_url": "https://x.invalid", "max_retries": 2}


def test_client_accepts_a_missing_base_url(monkeypatch, _isolated_client_cache) -> None:
    """주소가 없으면(None) SDK 기본 주소를 쓴다 — **진짜 SDK로 확인한다.**

    이 공급자만의 경로다. DeepSeek은 등록부가 언제나 주소를 주지만 여기는 None이 기본값이라,
    실제로 그 값을 받아주는지는 목으로는 알 수 없다. 목으로만 두면 운영에서 첫 호출에 터진다.

    네트워크는 쓰지 않는다 — 객체를 만들기만 한다.
    """
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(base_url=None))

    client = anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert isinstance(client, AsyncAnthropic)
    assert str(client.base_url).startswith("https://")  # 빈 주소가 아니라 기본 주소가 잡혔다


def test_client_is_reused_per_provider(monkeypatch, _isolated_client_cache) -> None:
    """호출마다 새로 만들면 커넥션 풀·TLS 세션이 매번 버려진다."""
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds())

    first = anthropic_sdk._client(PROVIDER_ANTHROPIC)
    second = anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert first is second


def test_client_is_rebuilt_when_settings_change(monkeypatch, _isolated_client_cache) -> None:
    """키가 바뀌면 새 클라이언트를 만든다(캐시 이름표가 설정 변화를 감지해야 한다)."""
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key="key-1"))
    first = anthropic_sdk._client(PROVIDER_ANTHROPIC)

    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key="key-2"))
    second = anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert first is not second


def test_client_is_rebuilt_when_the_address_changes(monkeypatch, _isolated_client_cache) -> None:
    """**주소**가 바뀌어도 새 클라이언트를 만든다(KNK-676 리뷰 P3).

    윗 테스트는 키만 바꾼다 — 캐시 이름표에서 주소를 빼도 전부 통과한다(변이로 확인).
    그러면 `ANTHROPIC_API_URL`을 바꿔도 옛 주소를 보는 클라이언트가 계속 쓰인다.
    """
    same_key = "same-key"
    monkeypatch.setattr(
        registry, "credentials", lambda provider: _creds(api_key=same_key, base_url="https://a.invalid")
    )
    first = anthropic_sdk._client(PROVIDER_ANTHROPIC)

    monkeypatch.setattr(
        registry, "credentials", lambda provider: _creds(api_key=same_key, base_url="https://b.invalid")
    )
    second = anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert first is not second


def test_cache_label_never_holds_the_raw_key(monkeypatch, _isolated_client_cache) -> None:
    """캐시 이름표에 키 원문을 넣지 않는다.

    Sentry는 오류 시 그 시점의 지역변수 값을 함께 싣는다 — 이름표에 키가 있으면 그대로 전송된다.
    """
    secret = "sk-ant-super-secret-value"
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key=secret))

    anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert all(secret not in str(label) for label in anthropic_sdk._clients)


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_key_is_a_config_error(monkeypatch, _isolated_client_cache, blank: str) -> None:
    """키가 비면(공백 포함) 설정 오류로 막는다 — SDK 생성자 예외가 번역 없이 새면 안 된다.

    어느 env를 채우라는 것인지도 메시지에 있어야 한다.
    """
    monkeypatch.setattr(registry, "credentials", lambda provider: _creds(api_key=blank))

    with pytest.raises(LlmConfigError) as exc_info:
        anthropic_sdk._client(PROVIDER_ANTHROPIC)

    assert "ANTHROPIC_API_KEY" in str(exc_info.value)
