"""선택지 생성 서비스 — '항상 정확히 3개 보장' 로직 검증.

LLM(_client)을 모킹해, 정상/부족/초과/중복/완전실패 경우 모두 choices가 정확히 3개가
되는지, 누적 재호출·결정적 폴백·retry_count·토큰 합산이 맞는지 확인한다.
"""

import json

import pytest
from openai import OpenAIError

from src.schemas.chat_turn import ChatStartSettings, ChatStorySettings, ChatTurnRequest
from src.services import chat_choices
from src.services.chat_choices import _FALLBACK, generate_choices


@pytest.fixture(autouse=True)
def _silence_sentry(monkeypatch):
    # 실패 경로가 Sentry를 부르지 않도록 noop으로 막는다(단위 테스트 격리).
    monkeypatch.setattr(chat_choices, "capture_ai_exception", lambda *a, **k: None)


def _request() -> ChatTurnRequest:
    return ChatTurnRequest(
        genre="판타지",
        story_settings=ChatStorySettings(
            world_setting="# 세계관\n아르덴 왕국.",
            character_setting="# 등장인물\n## 레이\n냉정하다.",
            user_role_setting="# 주인공\n카이",
            rule_setting="# 전개 규칙\n빌드업 후.",
        ),
        start_settings=ChatStartSettings(name="밤", prologue="깊은 밤.", start_situation="레이가 들어선다."),
        history=[],
        user_input="용건이 뭐요?",
        summary="",
    )


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, content, usage=None):
        self.choices = [_Choice(content)]
        self.model = "deepseek-v4-flash"
        self.usage = usage


def _mock_calls(monkeypatch, items: list):
    """호출마다 items를 순서대로 반환한다. item이 Exception이면 raise, str(json)이면 응답.

    items가 모자라면 마지막 item을 반복 사용한다(재호출이 같은 결과를 주는 상황 모사).
    """
    state = {"i": 0}

    async def _create(**kwargs):
        i = state["i"]
        state["i"] += 1
        item = items[i] if i < len(items) else items[-1]
        if isinstance(item, Exception):
            raise item
        return _Resp(item, usage=_Usage(5, 7))

    monkeypatch.setattr(chat_choices._client.chat.completions, "create", _create)
    return state


async def test_first_call_gives_three(monkeypatch) -> None:
    _mock_calls(monkeypatch, ['{"choices": ["맞선다", "피한다", "살핀다"]}'])
    res = await generate_choices(_request(), "*레이가 다가선다.*")
    assert res.choices == ["맞선다", "피한다", "살핀다"]
    assert res.retry_count == 0
    assert res.input_tokens == 5 and res.output_tokens == 7


async def test_accumulates_across_refill(monkeypatch) -> None:
    # 2개 → (재호출) 1개 = 누적 3개. retry_count=1, 토큰 합산.
    state = _mock_calls(
        monkeypatch,
        ['{"choices": ["맞선다", "피한다"]}', '{"choices": ["설득한다"]}'],
    )
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == ["맞선다", "피한다", "설득한다"]
    assert res.retry_count == 1
    assert state["i"] == 2  # 첫 호출 + 재호출 1회
    assert res.input_tokens == 10 and res.output_tokens == 14  # 5+5, 7+7


async def test_pads_with_fallback_when_short(monkeypatch) -> None:
    # 매번 같은 1개만 줘서 누적이 안 늘면, 재호출 2회 후 폴백으로 채워 정확히 3개.
    _mock_calls(monkeypatch, ['{"choices": ["맞선다"]}'])
    res = await generate_choices(_request(), "*장면*")
    assert len(res.choices) == 3
    assert res.choices[0] == "맞선다"
    assert res.choices[1] in _FALLBACK and res.choices[2] in _FALLBACK
    assert res.retry_count == 2  # 재호출을 최대치까지 시도한 뒤 보정


async def test_total_failure_absorbed_to_fallback(monkeypatch) -> None:
    # 호출이 매번 터져도 턴은 안 깨지고, 폴백 3개로 정확히 채운다.
    _mock_calls(monkeypatch, [OpenAIError("boom")])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == list(_FALLBACK)
    assert res.retry_count == 2
    assert res.input_tokens is None and res.output_tokens is None  # 성공한 호출 없음


async def test_failure_capture_carries_latency_and_retry(monkeypatch) -> None:
    # AN-4-8 — 실패 캡처마다 그 시도의 retry_count와 latency_ms가 실린다(KNK-529).
    calls: list = []
    monkeypatch.setattr(chat_choices, "capture_ai_exception", lambda *a, **k: calls.append(k))
    _mock_calls(monkeypatch, [OpenAIError("boom")])
    await generate_choices(_request(), "*장면*")
    assert [c["retry_count"] for c in calls] == [0, 1, 2]  # 첫 호출 + 재호출 2회 전부 캡처
    assert all(isinstance(c["latency_ms"], int) and c["latency_ms"] >= 0 for c in calls)


async def test_truncates_when_too_many(monkeypatch) -> None:
    _mock_calls(monkeypatch, ['{"choices": ["a", "b", "c", "d", "e"]}'])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == ["a", "b", "c"]  # 앞 3개만
    assert res.retry_count == 0


async def test_dedups_within_response(monkeypatch) -> None:
    # 한 응답 안의 중복은 제거하고 서로 다른 3개를 남긴다.
    _mock_calls(monkeypatch, ['{"choices": ["a", "a", "b", "c"]}'])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == ["a", "b", "c"]
    assert res.retry_count == 0


async def test_bad_json_then_fallback(monkeypatch) -> None:
    # json이 깨져도 흡수하고 폴백으로 3개를 보장한다.
    _mock_calls(monkeypatch, ["not json at all"])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == list(_FALLBACK)
    assert res.retry_count == 2


# ── _call 구조 방어 4분기 (KNK-574 감사 1-1) ────────────────────────────────
# _call은 응답이 계약을 어기면 ValueError를 던지고, generate_choices가 이를 흡수해
# 폴백으로 간다. JSONDecodeError 외 3분기(빈 content·비-dict·choices 부재)를 직접 태운다.
async def test_call_strips_code_fence(monkeypatch) -> None:
    # 코드펜스로 감싼 정상 응답도 펜스를 벗겨 파싱해야 한다(_strip_code_fence 실경로).
    _mock_calls(monkeypatch, ['```json\n{"choices": ["a", "b", "c"]}\n```'])
    choices, model, _in, _out = await chat_choices._call("sys", "user")
    assert choices == ["a", "b", "c"]


@pytest.mark.parametrize(
    "content",
    [
        "",  # 빈 content
        '["a", "b"]',  # 유효 JSON이나 dict가 아님(배열)
        '{"foo": 1}',  # dict지만 choices 키 부재
        '{"choices": "abc"}',  # choices가 list가 아님
    ],
)
async def test_call_raises_on_malformed_structure(monkeypatch, content) -> None:
    _mock_calls(monkeypatch, [content])
    with pytest.raises(ValueError):
        await chat_choices._call("sys", "user")


async def test_malformed_structure_absorbed_to_fallback(monkeypatch) -> None:
    # 위 구조 위반이 generate_choices까지 오면 흡수돼 폴백 3개로 수렴한다(배열 케이스 대표).
    _mock_calls(monkeypatch, ['["a", "b"]'])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == list(_FALLBACK)
    assert res.retry_count == 2


# ── 호출 인자 계약 단언 (KNK-584 재감사 #8) ───────────────────────────────────
# 가짜가 kwargs를 버리면 model·json 모드·max_tokens·thinking 회귀를 못 잡는다.
# 넘긴 인자를 붙잡아 선택지 호출 계약(비스트리밍 json 단발)을 고정한다.
async def test_call_contract(monkeypatch) -> None:
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return _Resp('{"choices": ["a", "b", "c"]}', usage=_Usage(5, 7))

    monkeypatch.setattr(chat_choices._client.chat.completions, "create", _create)
    await chat_choices._call("SYS", "USER")

    assert captured["model"] == chat_choices.settings.deepseek_chat_model
    assert captured["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["max_tokens"] == chat_choices._MAX_TOKENS
    assert captured["extra_body"] == chat_choices._THINKING_DISABLED
    assert "stream" not in captured  # 선택지는 비스트리밍 단발 호출


# ── 사건 재료 치환 (KNK-485, §5-3-5 선택지 3구성 재료) ──────────────────────
def test_build_user_replaces_event_material_slots() -> None:
    from src.schemas.chat_turn import MainEvent, TargetMainEvent

    req = _request().model_copy(
        update={
            "main_events": [
                MainEvent(name="반란의 서막", description="귀족 연합.", key_sentence="증거를 손에 넣는다.")
            ],
            "target_main_event": TargetMainEvent(name="반란의 서막", progress_turns=1),
            "occurred_main_event_names": ["선왕의 죽음"],
        }
    )
    # 템플릿에 슬롯이 아직 없어도 치환 맵 자체는 재료를 만들어야 한다(무해한 no-op).
    user = chat_choices._build_user(req, "*장면*")
    template_has_slots = "{{main_events}}" in chat_choices._USER_TEMPLATE
    if template_has_slots:
        assert "반란의 서막" in user and "- 선왕의 죽음" in user
    assert "{{main_events}}" not in user  # 미치환 슬롯이 남지 않는다
