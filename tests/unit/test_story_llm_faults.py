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


# stories 계약(3편 × id·storyline·recommended_infos 3개)을 지키는 유효한 스토리라인 응답.
_VALID_STORYLINES_JSON = (
    '{"stories": ['
    '{"id": 1, "storyline": "본문1", "recommended_infos": ["a", "b", "c"]},'
    '{"id": 2, "storyline": "본문2", "recommended_infos": ["a", "b", "c"]},'
    '{"id": 3, "storyline": "본문3", "recommended_infos": ["a", "b", "c"]}]}'
)


# ── 호출 인자 계약 단언 (KNK-584 재감사 #8) ───────────────────────────────────
# 가짜가 kwargs를 버리면 model·json 모드·temperature·max_tokens 회귀를 못 잡는다.
# 넘긴 인자를 붙잡아, compile은 pro(기본)·storylines는 flash로 호출하고 나머지 인자는
# 공통임을 고정한다(모델 오배선·인자 누락 방지).
def _capture(store: dict, content: str = '{"meta": {"title": "t"}}'):
    async def _create(**kwargs):
        store.update(kwargs)
        return _Resp(content)

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
    # storylines 경로는 stories 계약 검증(_validate_storylines)을 타므로 유효한 결과를 돌려준다.
    monkeypatch.setattr(
        story_llm._client.chat.completions, "create", _capture(captured, _VALID_STORYLINES_JSON)
    )
    await story_llm.generate_storylines("SYS", "USER")

    assert captured["model"] == story_llm.settings.storylines_model  # storylines = flash
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["extra_body"] == story_llm._THINKING_DISABLED


# ── invalid 응답 재호출 (KNK-312) ─────────────────────────────────────────────
# 실측된 버그(Sentry PYTHON-FASTAPI-5)의 재현: 본문 속 대사 인용부호가 JSON 이스케이프
# 없이 출력돼 파싱이 깨진다(finish_reason=stop — 잘림 아님). invalid 응답만 재호출하고,
# provider 오류는 재호출하지 않으며, 토큰은 실패 시도분까지 합산됨을 고정한다.
_UNESCAPED_QUOTE_JSON = (
    '{"stories": [{"id": 1, "storyline": "그들은 속삭였다. "너는 선택받은 자다." '
    '그 후로 꿈을 꿨다.", "recommended_infos": ["a", "b", "c"]}]}'
)


def _returns_sequence(contents: list):
    """호출마다 다음 content를 돌려주는 fake. 호출 횟수를 세기 위해 리스트를 소비한다."""
    calls = {"count": 0}

    async def _create(**kwargs):
        content = contents[calls["count"]]
        calls["count"] += 1
        if isinstance(content, BaseException):
            raise content
        return _Resp(content)

    return _create, calls


async def test_invalid_json_retries_then_succeeds(monkeypatch, captures) -> None:
    """1차 깨진 JSON(따옴표 미이스케이프) → 2차 정상: 200 경로로 회복, 토큰 합산·재호출 횟수 기록."""
    create, calls = _returns_sequence([_UNESCAPED_QUOTE_JSON, '{"meta": {"title": "t"}}'])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    parsed, usage = await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert parsed == {"meta": {"title": "t"}}
    assert calls["count"] == 2
    assert usage.retry_count == 1
    # 실패 시도분 토큰도 합산된다(시도당 in=11/out=13)
    assert usage.input_tokens == 22 and usage.output_tokens == 26
    # 재호출로 회복해도 실패는 Sentry에 남는다(발생 빈도 관측)
    assert len(captures) == 1
    assert captures[0]["error_code"] == ERROR_INVALID_AI_RESPONSE
    assert captures[0]["retry_count"] == 0  # 첫 시도의 실패


async def test_invalid_json_exhausts_retries_returns_502(monkeypatch, captures) -> None:
    """3회 전부 invalid면 502. 시도마다 Sentry 캡처가 남는다."""
    create, calls = _returns_sequence(['{"broken', '{"broken', '{"broken'])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert ei.value.status_code == 502
    assert "올바른 형식" in ei.value.detail
    assert calls["count"] == 3  # 첫 호출 + 재호출 2회
    assert [c["retry_count"] for c in captures] == [0, 1, 2]
    # 실패 예외에 실제 재호출 횟수가 실린다 — 엔드포인트가 실패 트레이스에도 기록(Codex 리뷰 F2)
    assert ei.value.retry_count == 2


async def test_provider_error_is_not_retried(monkeypatch, captures) -> None:
    """provider 오류(429)는 재호출 예산이 있어도 즉시 502 — invalid 응답만 재호출 대상."""
    exc = RateLimitError("rate", response=httpx.Response(429, request=_req()), body=None)
    create, calls = _returns_sequence([exc, '{"meta": {"title": "t"}}'])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert ei.value.status_code == 502
    assert calls["count"] == 1  # 재호출 없음
    assert captures[-1]["error_code"] == ERROR_PROVIDER_RATE_LIMITED
    assert ei.value.retry_count == 0  # 재호출이 없었으므로 0이 사실


async def test_generate_storylines_retries_twice_on_invalid(monkeypatch, captures) -> None:
    """스토리라인 경로가 재호출 2회(총 3회 호출)로 배선됐는지 고정한다(KNK-312)."""
    create, calls = _returns_sequence(['{"broken', '{"broken', '{"broken'])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    with pytest.raises(HTTPException) as ei:
        await story_llm.generate_storylines("SYS", "USER")

    assert ei.value.status_code == 502
    assert calls["count"] == 3


async def test_retry_gives_up_after_deadline(monkeypatch, captures) -> None:
    """재호출 예산이 남아도 전체 경과가 상한을 넘겼으면 포기한다(백엔드 90초 대기 한도).

    상한을 0초로 낮춰 '이미 시간 초과' 상황을 재현한다 — 유효하지 않은 첫 응답 후
    재호출 없이 즉시 502가 나야 한다.
    """
    monkeypatch.setattr(story_llm, "_INVALID_RETRY_DEADLINE_SECONDS", 0.0)
    create, calls = _returns_sequence(['{"broken', '{"meta": {"title": "t"}}'])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert ei.value.status_code == 502
    assert calls["count"] == 1  # 상한 초과 — 재호출하지 않음
    assert ei.value.retry_count == 0


async def test_retry_attempt_timeout_shrinks_to_remaining_budget(monkeypatch, captures) -> None:
    """재호출 시도의 호출 타임아웃은 전체 예산(90초)의 남은 시간으로 줄어든다(Codex P2).

    60초 직전에 시작한 재호출이 자체 90초 타임아웃으로 총 149초까지 끌지 못하게 하는
    방어다. 첫 시도 타임아웃은 90초 이하, 재호출 시도는 첫 시도보다 반드시 작아야 한다.
    """
    timeouts: list[float] = []

    async def _create(**kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            return _Resp('{"broken')  # 1차: 깨진 JSON → 재호출 유도
        return _Resp('{"meta": {"title": "t"}}')  # 2차: 정상

    monkeypatch.setattr(story_llm._client.chat.completions, "create", _create)

    await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert len(timeouts) == 2
    assert timeouts[0] <= story_llm._TOTAL_CALL_BUDGET_SECONDS
    assert timeouts[1] < timeouts[0]  # 재호출은 남은 예산만 사용


# ── stories 계약 검증 → invalid 재호출 (KNK-312, Sentry PYTHON-FASTAPI-A) ─────
# 파싱은 성공하지만 내용이 계약과 어긋나는 응답의 재현: 실제 장애에서는 LLM이
# recommended_infos를 각 항목이 아니라 최상위에 두어, 재호출 루프를 통과한 뒤
# 응답 조립(ValidationError)에서 500이 났다. 이제 루프 안 검증으로 재호출을 탄다.
_SCHEMA_MISMATCH_JSON = (
    '{"stories": ['
    '{"id": 1, "storyline": "본문1"},'
    '{"id": 2, "storyline": "본문2"},'
    '{"id": 3, "storyline": "본문3"}],'
    ' "recommended_infos": [["a"], ["b"], ["c"]]}'
)


async def test_storylines_schema_mismatch_retries_then_succeeds(monkeypatch, captures) -> None:
    """1차 스키마 불일치(FASTAPI-A 모양) → 2차 정상: 500 없이 회복하고 재호출 횟수를 기록한다."""
    create, calls = _returns_sequence([_SCHEMA_MISMATCH_JSON, _VALID_STORYLINES_JSON])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    result, usage = await story_llm.generate_storylines("SYS", "USER")

    assert [s["id"] for s in result["stories"]] == [1, 2, 3]
    assert calls["count"] == 2
    assert usage.retry_count == 1
    assert captures[-1]["error_code"] == ERROR_INVALID_AI_RESPONSE


@pytest.mark.parametrize(
    "content",
    [
        '{"result": "ok"}',  # stories 키 없음
        (
            '{"stories": ['
            '{"id": 1, "storyline": "s", "recommended_infos": ["a", "b", "c"]},'
            '{"id": 2, "storyline": "s", "recommended_infos": ["a", "b", "c"]}]}'
        ),  # 3편이 아님(2편) — 개수 강제
        '{"stories": ["문자열", "문자열", "문자열"]}',  # 항목이 객체가 아님
        _SCHEMA_MISMATCH_JSON,  # 항목에 recommended_infos 누락(FASTAPI-A)
        (
            '{"stories": ['
            '{"id": 1, "storyline": "s", "recommended_infos": ["a", "b"]},'
            '{"id": 2, "storyline": "s", "recommended_infos": ["a", "b", "c"]},'
            '{"id": 3, "storyline": "s", "recommended_infos": ["a", "b", "c"]}]}'
        ),  # 추천 추가 정보가 3개가 아님
        (
            '{"stories": ['
            '{"id": 1, "storyline": ["리스트"], "recommended_infos": ["a", "b", "c"]},'
            '{"id": 2, "storyline": "s", "recommended_infos": ["a", "b", "c"]},'
            '{"id": 3, "storyline": "s", "recommended_infos": ["a", "b", "c"]}]}'
        ),  # 필드 타입 위반(storyline이 문자열 아님) — 얕은 키 검사로의 회귀 방지(적대 리뷰 #2)
        (
            '{"stories": ['
            '{"id": 1, "storyline": "s", "recommended_infos": [1, 2, 3]},'
            '{"id": 2, "storyline": "s", "recommended_infos": ["a", "b", "c"]},'
            '{"id": 3, "storyline": "s", "recommended_infos": ["a", "b", "c"]}]}'
        ),  # 필드 타입 위반(recommended_infos 원소가 문자열 아님)
    ],
)
async def test_storylines_contract_violation_exhausts_to_502(
    monkeypatch, captures, content
) -> None:
    """계약 위반이 3회 연속이면 500(ValidationError)이 아니라 다른 invalid와 같은 502."""
    create, calls = _returns_sequence([content, content, content])
    monkeypatch.setattr(story_llm._client.chat.completions, "create", create)

    with pytest.raises(HTTPException) as ei:
        await story_llm.generate_storylines("SYS", "USER")

    assert ei.value.status_code == 502
    assert "올바른 형식" in ei.value.detail
    assert calls["count"] == 3  # 첫 호출 + 재호출 2회
    assert ei.value.retry_count == 2
