"""story_llm._complete_json 장애주입 테스트 (KNK-574 감사 1-1).

story 방어 심장부(_complete_json)를 monkeypatch로 우회하지 않고, LLM 클라이언트
(_client.chat.completions.create)를 fake로 교체해 **실제 방어 경로를 태운다**.
빈 응답·비객체 JSON·깨진 JSON·provider 예외가 모두 502로 분류되고, 사용자 노출
detail과 Sentry error_code가 실패 코드 카탈로그(AN-4-7)대로 실리는지 고정한다.
코드펜스로 감싼 정상 응답은 펜스를 벗겨 통과(200 경로)함을 함께 확인한다.
"""

import httpx
import pytest
from fastapi import HTTPException
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)

from src.core.sentry import (
    ERROR_INVALID_AI_RESPONSE,
    ERROR_PROVIDER_BAD_REQUEST,
    ERROR_PROVIDER_RATE_LIMITED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_UNAVAILABLE,
)
from src.services import story_llm


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/v1")


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    def __init__(self, p=11, c=13):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.model = "deepseek-v4-pro"
        self.usage = _Usage()


def _returns(content):
    async def _create(**kwargs):
        return _Resp(content)

    return _create


def _raises(exc):
    async def _create(**kwargs):
        raise exc

    return _create


@pytest.fixture
def captures(monkeypatch):
    """실패 캡처(Sentry)를 가로채 error_code 등 인자를 기록한다(실제 전송 없음)."""
    recorded: list = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda *a, **k: recorded.append(k))
    return recorded


# ── 비객체·빈·깨진 응답 → 502 invalid_ai_response ─────────────────────────────
@pytest.mark.parametrize(
    "content",
    [
        "",  # 빈 content
        None,  # content=None
        '["a", "b"]',  # 유효 JSON이나 dict가 아님(배열)
        '{"broken',  # 깨진 JSON(JSONDecodeError)
    ],
)
async def test_bad_content_returns_502_invalid(monkeypatch, captures, content) -> None:
    monkeypatch.setattr(story_llm._client.chat.completions, "create", _returns(content))
    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user")
    assert ei.value.status_code == 502
    assert "올바른 형식" in ei.value.detail  # 사용자 노출 detail(원문 미포함)
    assert captures[-1]["error_code"] == ERROR_INVALID_AI_RESPONSE


# ── provider 예외 → error_code별 502 + 해당 detail ────────────────────────────
@pytest.mark.parametrize(
    "exc, code, detail_sub",
    [
        (APITimeoutError(request=_req()), ERROR_PROVIDER_TIMEOUT, "시간이 초과"),
        (
            RateLimitError("rate", response=httpx.Response(429, request=_req()), body=None),
            ERROR_PROVIDER_RATE_LIMITED,
            "일시적으로 제한",
        ),
        (
            BadRequestError("bad", response=httpx.Response(400, request=_req()), body=None),
            ERROR_PROVIDER_BAD_REQUEST,
            "거부",
        ),
        (APIConnectionError(request=_req()), ERROR_PROVIDER_UNAVAILABLE, "연동 중 오류"),
    ],
)
async def test_provider_error_returns_502(monkeypatch, captures, exc, code, detail_sub) -> None:
    monkeypatch.setattr(story_llm._client.chat.completions, "create", _raises(exc))
    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user")
    assert ei.value.status_code == 502
    assert detail_sub in ei.value.detail
    assert captures[-1]["error_code"] == code


# ── 코드펜스로 감싼 정상 응답 → 펜스 벗기고 통과(200 경로) ─────────────────────
async def test_code_fenced_json_passes(monkeypatch, captures) -> None:
    fenced = '```json\n{"meta": {"title": "제목"}}\n```'
    monkeypatch.setattr(story_llm._client.chat.completions, "create", _returns(fenced))
    parsed, usage = await story_llm._complete_json("sys", "user")
    assert parsed == {"meta": {"title": "제목"}}  # _strip_code_fence가 실제로 실행됨
    assert usage.model == "deepseek-v4-pro"
    assert usage.input_tokens == 11 and usage.output_tokens == 13
    assert captures == []  # 성공 경로이므로 실패 캡처 없음


# ── malformed SDK 응답 모양 → 502 invalid (재감사 #4) ─────────────────────────
# 빈 content(위)와 달리, 응답 '껍데기'가 깨진 경계다: choices[0].message.content 접근에서
# IndexError(빈 choices)·AttributeError(message=None)가 나도 500이 아니라 정제 502로
# 수렴해야 한다. chat_choices·chat_judgement는 이미 이 둘을 잡는데 story만 빠져 있었다(F2 대칭).
class _NoChoicesResp:
    choices: list = []
    model = "deepseek-v4-pro"
    usage = _Usage()


class _NoneMsgChoice:
    message = None


class _NoneMsgResp:
    choices = [_NoneMsgChoice()]
    model = "deepseek-v4-pro"
    usage = _Usage()


@pytest.mark.parametrize("resp", [_NoChoicesResp(), _NoneMsgResp()])
async def test_malformed_sdk_shape_returns_502_invalid(monkeypatch, captures, resp) -> None:
    async def _create(**kwargs):
        return resp

    monkeypatch.setattr(story_llm._client.chat.completions, "create", _create)
    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user")
    assert ei.value.status_code == 502
    assert "올바른 형식" in ei.value.detail  # invalid_ai_response detail(원문 미포함)
    assert captures[-1]["error_code"] == ERROR_INVALID_AI_RESPONSE


# ── 호출 인자 계약 단언 (KNK-584 재감사 #8) ───────────────────────────────────
# 가짜가 kwargs를 버리면 model·json 모드·temperature·max_tokens 회귀를 못 잡는다.
# 넘긴 인자를 붙잡아, compile은 pro(기본)·storylines는 flash로 호출하고 나머지 인자는
# 공통임을 고정한다(모델 오배선·인자 누락 방지).
def _capture(store: dict):
    async def _create(**kwargs):
        store.update(kwargs)
        return _Resp('{"meta": {"title": "t"}}')

    return _create


async def test_complete_json_call_contract_compile(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(story_llm._client.chat.completions, "create", _capture(captured))
    await story_llm._complete_json("SYS", "USER")  # model 미지정 → 컴파일 기본

    assert captured["model"] == story_llm.settings.story_compile_model  # compile 기본 = pro
    assert captured["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == story_llm._TEMPERATURE
    assert captured["max_tokens"] == story_llm._MAX_TOKENS
    assert captured["extra_body"] == story_llm._THINKING_DISABLED


async def test_generate_storylines_uses_flash_model(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(story_llm._client.chat.completions, "create", _capture(captured))
    await story_llm.generate_storylines("SYS", "USER")

    assert captured["model"] == story_llm.settings.storylines_model  # storylines = flash
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["extra_body"] == story_llm._THINKING_DISABLED
