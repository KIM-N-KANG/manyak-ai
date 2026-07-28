"""story_llm._complete_json 장애주입 테스트 (KNK-574 감사 1-1).

story 방어 심장부(_complete_json)를 monkeypatch로 우회하지 않고, **SDK 경계**에 fake를 심어
실제 방어 경로를 태운다. 목 지점은 통로 이관(KNK-672) 후 어댑터 아래로 내려갔다 — 모델
등록부·어댑터의 인자 조립·응답 해석·예외 번역까지 전부 실제 코드가 돈다.
빈 응답·비객체 JSON·깨진 JSON·provider 예외가 모두 502로 분류되고, 사용자 노출
detail과 Sentry error_code가 실패 코드 카탈로그(AN-4-7)대로 실리는지 고정한다.
코드펜스로 감싼 정상 응답은 펜스를 벗겨 통과(200 경로)함을 함께 확인한다.
"""

from types import SimpleNamespace

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
from src.services.llm import openai_sdk
from src.services.llm.base import LlmConfigError


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/v1")


def _install(monkeypatch, create) -> None:
    """SDK 경계에 fake를 심는다 — 어댑터가 쓰는 클라이언트 자체를 가짜로 바꾼다.

    통로 위(story_llm)나 어댑터 함수를 가로채면 인자 조립·응답 해석·예외 번역이 통째로
    건너뛰어져, 정작 이관에서 깨지기 쉬운 부분을 검증하지 못한다.
    """
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai_sdk, "_client", lambda provider: client)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)
        # 실제 SDK 응답에는 항상 있는 칸이다. 빼두면 어댑터가 매 호출 꺼내기에 실패해
        # 스택트레이스 경고를 쏟고, 정상 추출 경로는 한 번도 검증되지 않는다(KNK-673 리뷰).
        self.finish_reason = "stop"


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


def _returns_response(resp):
    """이미 만들어 둔 응답 객체를 그대로 돌려주는 fake(_returns는 content로 만든다)."""

    async def _create(**kwargs):
        return resp

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
    _install(monkeypatch, _returns(content))
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
    _install(monkeypatch, _raises(exc))
    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user")
    assert ei.value.status_code == 502
    assert detail_sub in ei.value.detail
    assert captures[-1]["error_code"] == code


# ── 코드펜스로 감싼 정상 응답 → 펜스 벗기고 통과(200 경로) ─────────────────────
async def test_code_fenced_json_passes(monkeypatch, captures) -> None:
    fenced = '```json\n{"meta": {"title": "제목"}}\n```'
    _install(monkeypatch, _returns(fenced))
    parsed, usage = await story_llm._complete_json("sys", "user")
    assert parsed == {"meta": {"title": "제목"}}  # _strip_code_fence가 실제로 실행됨
    assert usage.model == "deepseek-v4-pro"
    assert usage.provider == "deepseek"  # 모델 이름을 등록부가 해석한 값(KNK-674)
    assert usage.input_tokens == 11 and usage.output_tokens == 13
    assert captures == []  # 성공 경로이므로 실패 캡처 없음


# ── malformed SDK 응답 모양 → 502 invalid (재감사 #4) ─────────────────────────
# 빈 content(위)와 달리, 응답 '껍데기'가 깨진 경계다: 빈 choices·message 없음처럼 본문을
# 꺼낼 수 없는 응답도 500이 아니라 정제 502로 수렴해야 한다.
# 통로 이관(KNK-672) 후 이 경로가 바뀌었다 — 예전에는 호출부가 IndexError·AttributeError를
# 잡았지만, 이제 어댑터가 그런 응답을 빈 본문으로 정규화하고 호출부의 "빈 응답" 판정이 받는다.
# 결과(502 invalid_ai_response)는 같다. 이 테스트는 그 결과가 유지되는지를 고정한다.
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

    _install(monkeypatch, _create)
    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user")
    assert ei.value.status_code == 502
    assert "올바른 형식" in ei.value.detail  # invalid_ai_response detail(원문 미포함)
    assert captures[-1]["error_code"] == ERROR_INVALID_AI_RESPONSE


# ── usage·model 칸이 없는 응답 → 예외 없이 통과 (KNK-672에서 바뀐 경계) ──────
# 이관 전에는 `response.usage`·`response.model`을 그냥 꺼내다 AttributeError가 나서
# invalid 재호출 → 502였다. 지금은 통로가 없는 칸을 None으로 두고 통과시킨다.
# **의도한 변경이다** — 백엔드 계약이 "토큰 누락 시 null"이고, 모델명은 요청 이름으로 채운다.
# 이 두 테스트가 없으면 어느 쪽으로 되돌아가도 아무도 못 잡는다.
#
# 여기서 보는 것은 `_complete_json` 계층까지다(HTTP 응답이 아니다). None 토큰이 응답 본문에
# 실제로 null로 실리는지는 엔드포인트 테스트가 따로 본다
# (tests/test_storylines_api.py::test_storylines_endpoint_serializes_missing_tokens_as_null).
class _NoUsageResp:
    """usage 속성 자체가 없는 응답(정상 본문은 있음)."""

    def __init__(self, content: str = '{"meta": {"title": "t"}}') -> None:
        self.choices = [_Choice(content)]
        self.model = "deepseek-v4-pro"


class _NoModelResp:
    """model 속성 자체가 없는 응답(정상 본문은 있음)."""

    def __init__(self, content: str = '{"meta": {"title": "t"}}') -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


async def test_missing_usage_passes_with_null_tokens(monkeypatch, captures) -> None:
    _install(monkeypatch, _returns_response(_NoUsageResp()))

    parsed, usage = await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert parsed == {"meta": {"title": "t"}}
    assert usage.input_tokens is None and usage.output_tokens is None  # 0이 아니라 null
    assert usage.retry_count == 0  # 재호출로 새지 않는다
    assert captures == []  # AI 호출 실패로 집계하지 않는다


async def test_missing_model_falls_back_to_requested_model(monkeypatch, captures) -> None:
    _install(monkeypatch, _returns_response(_NoModelResp()))

    parsed, usage = await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert usage.model == story_llm.settings.story_compile_model  # 요청에 쓴 이름으로 채움
    assert usage.input_tokens == 11 and usage.output_tokens == 13
    assert captures == []


# ── 설정 오류는 502로 위장되지 않는다 (KNK-672 이관 가드) ─────────────────────
async def test_unregistered_model_is_not_disguised_as_502(monkeypatch, captures) -> None:
    """등록부에 없는 모델은 provider 장애(502)가 아니라 설정 오류 그대로 드러난다.

    `except LlmError`가 설정 오류까지 삼키면 "모델 이름을 잘못 적었다"가 "LLM이 응답하지
    않았다"로 기록돼, 원인을 엉뚱한 곳에서 찾게 된다.
    """
    _install(monkeypatch, _returns('{"meta": {"title": "t"}}'))

    with pytest.raises(LlmConfigError):
        await story_llm._complete_json("sys", "user", model="deepseek-v9-imaginary")

    assert captures == []  # AI 호출 실패로 집계하지 않는다


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
    _install(monkeypatch, _capture(captured))
    await story_llm._complete_json("SYS", "USER")  # model 미지정 → 컴파일 기본

    assert captured["model"] == story_llm.settings.story_compile_model  # compile 기본 = pro
    assert captured["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == story_llm._TEMPERATURE
    assert captured["max_tokens"] == story_llm._MAX_TOKENS
    # 추론 끄기는 이제 story_llm이 아니라 등록부(use_thinking=False)의 뜻을 어댑터가 옮긴 것이다.
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_generate_storylines_uses_flash_model(monkeypatch) -> None:
    captured: dict = {}
    # storylines 경로는 stories 계약 검증(_validate_storylines)을 타므로 유효한 결과를 돌려준다.
    _install(monkeypatch, _capture(captured, _VALID_STORYLINES_JSON))
    await story_llm.generate_storylines("SYS", "USER")

    assert captured["model"] == story_llm.settings.storylines_model  # storylines = flash
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


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
    _install(monkeypatch, create)

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
    _install(monkeypatch, create)

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
    _install(monkeypatch, create)

    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert ei.value.status_code == 502
    assert calls["count"] == 1  # 재호출 없음
    assert captures[-1]["error_code"] == ERROR_PROVIDER_RATE_LIMITED
    assert ei.value.retry_count == 0  # 재호출이 없었으므로 0이 사실


async def test_generate_storylines_retries_twice_on_invalid(monkeypatch, captures) -> None:
    """스토리라인 경로가 재호출 2회(총 3회 호출)로 배선됐는지 고정한다(KNK-312)."""
    create, calls = _returns_sequence(['{"broken', '{"broken', '{"broken'])
    _install(monkeypatch, create)

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
    _install(monkeypatch, create)

    with pytest.raises(HTTPException) as ei:
        await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert ei.value.status_code == 502
    assert calls["count"] == 1  # 상한 초과 — 재호출하지 않음
    assert ei.value.retry_count == 0


class _FakeClock:
    """시각을 테스트가 직접 정하는 가짜 시계. `now`를 바꾸면 그때부터 그 값을 돌려준다.

    "몇 번째 호출이면 몇 초"가 아니라 **값**으로 움직인다 — 호출 횟수에 맞춰 목록을 짜두면
    나중에 로그 한 줄만 늘어도 시각이 통째로 밀려, 테스트가 엉뚱한 이유로 통과하거나 실패한다.

    `story_llm.time`을 통째로 이걸로 바꾼다 — 전역 `time` 모듈을 건드리면 pytest·asyncio까지
    영향을 받는다.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def _create_then_advance(clock: _FakeClock, elapsed: float, timeouts: list[float]):
    """1차는 깨진 JSON을 주면서 시계를 `elapsed`초 앞으로 돌리고, 2차는 정상 응답을 준다."""

    async def _create(**kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            clock.now = elapsed  # 이 호출에 그만큼 걸렸다고 친다
            return _Resp('{"broken')  # 깨진 JSON → 재호출 유도
        return _Resp('{"meta": {"title": "t"}}')

    return _create


async def test_retry_attempt_timeout_shrinks_to_remaining_budget(monkeypatch, captures) -> None:
    """재호출 시도의 호출 타임아웃은 전체 예산(90초)의 **남은 시간**으로 줄어든다(Codex P2).

    59초를 쓴 뒤의 재호출에는 31초만 줘야 한다. 그래야 60초 직전에 시작한 재호출이 자체
    90초 타임아웃으로 총 149초까지 끌지 못한다.

    시계를 고정해 **정확한 값**을 단언한다. "두 번째가 첫 번째보다 작다"만 보면 남은 시간을
    거의 안 깎는 잘못된 구현(`90 - 시도횟수 × 0.000001`)도 통과한다(Codex 변이 시험).
    """
    timeouts: list[float] = []
    clock = _FakeClock()
    monkeypatch.setattr(story_llm, "time", clock)
    _install(monkeypatch, _create_then_advance(clock, 59.0, timeouts))

    await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert timeouts == [90.0, 31.0]  # 첫 시도 90초, 59초 쓴 뒤 재호출은 남은 31초


async def test_retry_attempt_timeout_never_goes_below_one_second(monkeypatch, captures) -> None:
    """예산을 다 써도 타임아웃은 1초 미만으로 내려가지 않는다(0·음수를 SDK에 넘기지 않게).

    `_INVALID_RETRY_DEADLINE_SECONDS`(60초)를 넘기면 원래 재호출을 포기하므로, 그 상한을
    크게 올린 상태에서 하한이 실제로 걸리는지 본다.
    """
    timeouts: list[float] = []
    clock = _FakeClock()
    monkeypatch.setattr(story_llm, "time", clock)
    monkeypatch.setattr(story_llm, "_INVALID_RETRY_DEADLINE_SECONDS", 10_000.0)
    _install(monkeypatch, _create_then_advance(clock, 500.0, timeouts))

    await story_llm._complete_json("sys", "user", max_invalid_retries=2)

    assert timeouts == [90.0, 1.0]  # 예산을 한참 넘겨도 하한 1초


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


async def test_storylines_ids_normalized_to_sequence(monkeypatch, captures) -> None:
    """id가 중복·범위 밖(예: [1,1,99])이어도 재호출·502 없이 순서대로 1·2·3으로 교정한다.

    id 값은 표시·정렬용이라(선택은 본문 텍스트로) 어긋나도 무해 — 장르 덮어쓰기와 같은
    '계약 값은 코드가 담보' 패턴(적대 리뷰 #3, 코덱스 P2). 200으로 통과하되 id만 교정.
    """
    weird_ids = (
        '{"stories": ['
        '{"id": 1, "storyline": "본문1", "recommended_infos": ["a", "b", "c"]},'
        '{"id": 1, "storyline": "본문2", "recommended_infos": ["a", "b", "c"]},'
        '{"id": 99, "storyline": "본문3", "recommended_infos": ["a", "b", "c"]}]}'
    )
    create, calls = _returns_sequence([weird_ids])
    _install(monkeypatch, create)

    result, usage = await story_llm.generate_storylines("SYS", "USER")

    assert [s["id"] for s in result["stories"]] == [1, 2, 3]  # 순서대로 교정됨
    assert calls["count"] == 1  # 재호출 없음(무해한 이탈)
    assert usage.retry_count == 0
    assert captures == []  # 실패 캡처 없음(200 경로)


async def test_storylines_schema_mismatch_retries_then_succeeds(monkeypatch, captures) -> None:
    """1차 스키마 불일치(FASTAPI-A 모양) → 2차 정상: 500 없이 회복하고 재호출 횟수를 기록한다."""
    create, calls = _returns_sequence([_SCHEMA_MISMATCH_JSON, _VALID_STORYLINES_JSON])
    _install(monkeypatch, create)

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
    _install(monkeypatch, create)

    with pytest.raises(HTTPException) as ei:
        await story_llm.generate_storylines("SYS", "USER")

    assert ei.value.status_code == 502
    assert "올바른 형식" in ei.value.detail
    assert calls["count"] == 3  # 첫 호출 + 재호출 2회
    assert ei.value.retry_count == 2


# ── provider는 고정값이 아니라 지금 쓰는 모델의 공급자다 (KNK-674) ────────────
# 모든 테스트가 DeepSeek이면 "그냥 'deepseek'을 적어둔 코드"와 구분되지 않는다.
# 다른 회사 모델을 하나 끼워 넣어, 메타와 실패 태그가 함께 따라 바뀌는지 본다.
async def test_provider_follows_the_selected_model(monkeypatch, other_provider_model) -> None:
    other_provider_model()
    _install(monkeypatch, _returns('{"meta": {"title": "제목"}}'))

    _parsed, usage = await story_llm._complete_json("sys", "user", "not-deepseek-model")

    assert usage.provider == "not-deepseek"


async def test_failure_capture_provider_follows_the_selected_model(
    monkeypatch, other_provider_model, captures
) -> None:
    """실패 경로에도 같은 값이 실린다 — 여기가 비면 Sentry provider 태그가 거짓이 된다."""
    other_provider_model()
    _install(monkeypatch, _returns("이건 JSON이 아님"))

    with pytest.raises(HTTPException):
        await story_llm._complete_json("sys", "user", "not-deepseek-model")

    assert captures[-1]["provider"] == "not-deepseek"


def test_llm_usage_requires_an_explicit_provider() -> None:
    """`LlmUsage.provider`에 기본값을 두지 않는다(KNK-674 리뷰 M3).

    기본값을 붙이면 provider를 안 넘긴 호출부가 **에러 없이 조용히** 그 값을 물려받는다 —
    없애려던 전역 폴백이 이름만 바꿔 되살아난다. 선언은 주석에 있었지만 지키는 장치가 없어
    변이(`provider: str = "deepseek"`)가 그대로 통과했다.
    """
    with pytest.raises(TypeError):
        story_llm.LlmUsage("m", 1, 2)  # provider 없이 만들 수 없어야 한다


def test_llm_usage_provider_cannot_be_passed_by_position() -> None:
    """provider는 이름으로만 넘긴다(KNK-674 리뷰 L2).

    이 칸을 retry_count 앞에 끼워 넣었기 때문에, 위치로 넘길 수 있게 두면 예전 습관대로 쓴
    `LlmUsage("m", 1, 2, 3)`이 **에러 없이** 숫자 3을 공급자 이름으로 받아들이고 재호출
    횟수는 0이 된다. 그대로 백엔드에 `provider: 3`이 적재된다.
    """
    with pytest.raises(TypeError):
        story_llm.LlmUsage("m", 1, 2, 3)  # 4번째 위치 인자는 이제 막힌다

    # 이름을 적으면 옛 의미(재호출 3회) 그대로 만들어진다.
    usage = story_llm.LlmUsage("m", 1, 2, retry_count=3, provider="deepseek")
    assert usage.retry_count == 3 and usage.provider == "deepseek"
