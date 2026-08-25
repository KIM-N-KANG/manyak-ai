"""선택지 생성 서비스 — '항상 정확히 3개 보장' 로직 검증.

SDK 경계를 모킹해(통로 이관 후의 목 지점 — `install_llm_sdk`), 정상/부족/초과/중복/완전실패
경우 모두 choices가 정확히 3개가 되는지, 누적 재호출·결정적 폴백·retry_count·토큰 합산이
맞는지 확인한다.
"""

import json

import pytest
from openai import OpenAIError

from src.schemas.chat_turn import (
    ChatHistoryItem,
    ChatStartSettings,
    ChatStorySettings,
    ChatTurnRequest,
)
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
        # 실제 SDK 응답에는 항상 있는 칸이다. 빼두면 어댑터가 매 호출 꺼내기에 실패해
        # 스택트레이스 경고를 쏟고, 정상 추출 경로는 한 번도 검증되지 않는다(KNK-673 리뷰).
        self.finish_reason = "stop"


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, content, usage=None):
        self.choices = [_Choice(content)]
        self.model = "deepseek-v4-flash"
        self.usage = usage


def _mock_calls(install, items: list):
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

    install(_create)
    return state


async def test_first_call_gives_three(install_llm_sdk) -> None:
    _mock_calls(install_llm_sdk, ['{"choices": ["맞선다", "피한다", "살핀다"]}'])
    res = await generate_choices(_request(), "*레이가 다가선다.*")
    assert res.choices == ["맞선다", "피한다", "살핀다"]
    assert res.retry_count == 0
    assert res.input_tokens == 5 and res.output_tokens == 7


async def test_accumulates_across_refill(install_llm_sdk) -> None:
    # 2개 → (재호출) 1개 = 누적 3개. retry_count=1, 토큰 합산.
    state = _mock_calls(
        install_llm_sdk,
        ['{"choices": ["맞선다", "피한다"]}', '{"choices": ["설득한다"]}'],
    )
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == ["맞선다", "피한다", "설득한다"]
    assert res.retry_count == 1
    assert state["i"] == 2  # 첫 호출 + 재호출 1회
    assert res.input_tokens == 10 and res.output_tokens == 14  # 5+5, 7+7


async def test_pads_with_fallback_when_short(install_llm_sdk) -> None:
    # 매번 같은 1개만 줘서 누적이 안 늘면, 재호출 2회 후 폴백으로 채워 정확히 3개.
    _mock_calls(install_llm_sdk, ['{"choices": ["맞선다"]}'])
    res = await generate_choices(_request(), "*장면*")
    assert len(res.choices) == 3
    assert res.choices[0] == "맞선다"
    assert res.choices[1] in _FALLBACK and res.choices[2] in _FALLBACK
    assert res.retry_count == 2  # 재호출을 최대치까지 시도한 뒤 보정


async def test_total_failure_absorbed_to_fallback(install_llm_sdk) -> None:
    # 호출이 매번 터져도 턴은 안 깨지고, 폴백 3개로 정확히 채운다.
    _mock_calls(install_llm_sdk, [OpenAIError("boom")])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == list(_FALLBACK)
    assert res.retry_count == 2
    assert res.input_tokens is None and res.output_tokens is None  # 성공한 호출 없음
    # 성공한 호출이 하나도 없어도 provider는 채워진다 — 결과가 아니라 모델 이름으로
    # 정해지기 때문이다(KNK-674). 여기서 비면 meta.provider가 null로 나간다.
    assert res.provider == "deepseek"


async def test_failure_capture_carries_latency_and_retry(monkeypatch, install_llm_sdk) -> None:
    # AN-4-8 — 실패 캡처마다 그 시도의 retry_count와 latency_ms가 실린다(KNK-529).
    calls: list = []
    monkeypatch.setattr(chat_choices, "capture_ai_exception", lambda *a, **k: calls.append(k))
    _mock_calls(install_llm_sdk, [OpenAIError("boom")])
    await generate_choices(_request(), "*장면*")
    assert [c["retry_count"] for c in calls] == [0, 1, 2]  # 첫 호출 + 재호출 2회 전부 캡처
    assert all(isinstance(c["latency_ms"], int) and c["latency_ms"] >= 0 for c in calls)
    assert {c["provider"] for c in calls} == {"deepseek"}  # provider 태그도 함께(KNK-674)


async def test_truncates_when_too_many(install_llm_sdk) -> None:
    _mock_calls(install_llm_sdk, ['{"choices": ["a", "b", "c", "d", "e"]}'])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == ["a", "b", "c"]  # 앞 3개만
    assert res.retry_count == 0


async def test_dedups_within_response(install_llm_sdk) -> None:
    # 한 응답 안의 중복은 제거하고 서로 다른 3개를 남긴다.
    _mock_calls(install_llm_sdk, ['{"choices": ["a", "a", "b", "c"]}'])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == ["a", "b", "c"]
    assert res.retry_count == 0


async def test_bad_json_then_fallback(install_llm_sdk) -> None:
    # json이 깨져도 흡수하고 폴백으로 3개를 보장한다.
    _mock_calls(install_llm_sdk, ["not json at all"])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == list(_FALLBACK)
    assert res.retry_count == 2


# ── _call 구조 방어 4분기 (KNK-574 감사 1-1) ────────────────────────────────
# _call은 응답이 계약을 어기면 ValueError를 던지고, generate_choices가 이를 흡수해
# 폴백으로 간다. JSONDecodeError 외 3분기(빈 content·비-dict·choices 부재)를 직접 태운다.
async def test_call_strips_code_fence(install_llm_sdk) -> None:
    # 코드펜스로 감싼 정상 응답도 펜스를 벗겨 파싱해야 한다(_strip_code_fence 실경로).
    _mock_calls(install_llm_sdk, ['```json\n{"choices": ["a", "b", "c"]}\n```'])
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
async def test_call_raises_on_malformed_structure(install_llm_sdk, content) -> None:
    _mock_calls(install_llm_sdk, [content])
    with pytest.raises(ValueError):
        await chat_choices._call("sys", "user")


async def test_malformed_structure_absorbed_to_fallback(monkeypatch, install_llm_sdk) -> None:
    # 위 구조 위반이 generate_choices까지 오면 흡수돼 폴백 3개로 수렴한다(배열 케이스 대표).
    captures: list[dict] = []
    monkeypatch.setattr(
        chat_choices, "capture_ai_exception", lambda *args, **kwargs: captures.append(kwargs)
    )
    _mock_calls(install_llm_sdk, ['["a", "b"]'])
    res = await generate_choices(_request(), "*장면*")
    assert res.choices == list(_FALLBACK)
    assert res.retry_count == 2
    assert {capture["error_code"] for capture in captures} == {"invalid_ai_response"}


# ── 호출 인자 계약 단언 (KNK-584 재감사 #8) ───────────────────────────────────
# 가짜가 kwargs를 버리면 model·json 모드·max_tokens·thinking 회귀를 못 잡는다.
# 넘긴 인자를 붙잡아 선택지 호출 계약(비스트리밍 json 단발)을 고정한다.
async def test_call_contract(install_llm_sdk) -> None:
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return _Resp('{"choices": ["a", "b", "c"]}', usage=_Usage(5, 7))

    install_llm_sdk(_create)
    await chat_choices._call("SYS", "USER")

    assert captured["model"] == chat_choices.settings.chat_model
    assert captured["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["max_tokens"] == chat_choices._MAX_TOKENS
    # 추론 끄기는 이제 호출부가 아니라 등록부(use_thinking=False)의 뜻을 어댑터가 옮긴 것이다.
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    # 타임아웃을 호출마다 넘긴다 — 비우면 상한이 SDK 기본값(10분)으로 늘어난다(KNK-673).
    assert captured["timeout"] == chat_choices._TIMEOUT_SECONDS == 60.0
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


def test_build_user_removes_character_image_syntax_from_history_and_output() -> None:
    req = _request().model_copy(
        update={
            "history": [
                ChatHistoryItem(
                    role="ASSISTANT",
                    content=(
                        "[[세린:https://cdn.example.com/serin.webp]]세린: 기다렸어?"
                    ),
                )
            ]
        }
    )
    ai_output = "[character:미라]미라: 나도 왔어."

    user = chat_choices._build_user(req, ai_output)

    assert "[[세린:" not in user
    assert "https://cdn.example.com/serin.webp" not in user
    assert "[character:미라]" not in user
    assert "세린: 기다렸어?" in user
    assert "미라: 나도 왔어." in user


# ── 토큰 누락은 0이 아니라 null (KNK-673 리뷰) ───────────────────────────────
# 성공 경로의 가짜가 늘 usage를 채워 주면 "null을 0으로 바꾸는" 회귀를 못 잡는다
# (변이 `result.usage.input_tokens or 0`이 통과했다). 없는 경우를 따로 태운다.
async def test_missing_usage_stays_null(install_llm_sdk) -> None:
    async def _create(**kwargs):
        return _Resp('{"choices": ["a", "b", "c"]}', usage=None)  # usage 없는 응답

    install_llm_sdk(_create)
    res = await generate_choices(_request(), "*장면*")

    assert res.choices == ["a", "b", "c"]
    assert res.input_tokens is None and res.output_tokens is None  # 0이 아니라 null


# ── 전송 오류와 내용물 오류의 구분 (KNK-673 리뷰) ────────────────────────────
# 결과 모양(폴백 3개)만 보면 둘이 구분되지 않아, 깨진 JSON을 공급자 장애로 잘못 접어도
# 테스트가 통과했다. 그러면 Sentry 오류 이름이 invalid_ai_response에서
# provider_unavailable로 조용히 바뀌어 "LLM이 이상한 답을 줬다"가 "DeepSeek이 죽었다"가 된다.
async def test_broken_json_is_reported_as_invalid_ai_response(monkeypatch, install_llm_sdk) -> None:
    from src.core.sentry import ERROR_INVALID_AI_RESPONSE, classify_error_code

    captured: list = []
    monkeypatch.setattr(chat_choices, "capture_ai_exception", lambda e, **k: captured.append(e))
    _mock_calls(install_llm_sdk, ["not json at all"])

    await generate_choices(_request(), "*장면*")

    assert captured, "깨진 JSON도 Sentry로 보고돼야 한다"
    assert all(isinstance(e, json.JSONDecodeError) for e in captured)
    assert {classify_error_code(e) for e in captured} == {ERROR_INVALID_AI_RESPONSE}


async def test_our_own_bug_is_not_absorbed_into_the_fallback(monkeypatch, install_llm_sdk) -> None:
    """우리 코드의 결함은 폴백으로 덮지 않고 그대로 올려보낸다.

    흡수 대상은 전송 오류와 내용물 오류뿐이다. 예외 절을 넓히면(예전의 IndexError·
    AttributeError를 되넣거나 `except Exception`으로 바꾸면) 오타·형 실수까지 "선택지가 좀
    밋밋하네" 수준으로 위장돼, 500이 나야 알 수 있는 버그가 영영 안 보인다.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("우리 코드의 결함")

    monkeypatch.setattr(chat_choices, "_accumulate", _boom)
    _mock_calls(install_llm_sdk, ['{"choices": ["a", "b", "c"]}'])

    with pytest.raises(RuntimeError):
        await generate_choices(_request(), "*장면*")


# ── provider는 고정값이 아니라 지금 쓰는 모델의 공급자다 (KNK-674) ────────────
# 모든 테스트가 DeepSeek이면 "그냥 'deepseek'을 적어둔 코드"와 구분되지 않는다.
# 다른 회사 모델을 하나 끼워 넣어, 값이 모델을 따라 바뀌는지 본다.
async def test_provider_follows_the_selected_model(other_provider_model, install_llm_sdk) -> None:
    other_provider_model(chat_choices)
    _mock_calls(install_llm_sdk, ['{"choices": ["가", "나", "다"]}'])

    res = await generate_choices(_request(), "*장면*")

    assert res.provider == "not-deepseek"  # settings.llm_provider가 남아 있으면 "deepseek"이 된다


async def test_failure_capture_provider_follows_the_selected_model(
    monkeypatch, other_provider_model, install_llm_sdk
) -> None:
    """실패 태그도 같은 값이다 — 여기가 상수면 다른 회사의 실패가 deepseek 탓으로 쌓인다."""
    other_provider_model(chat_choices)
    calls: list = []
    monkeypatch.setattr(chat_choices, "capture_ai_exception", lambda *a, **k: calls.append(k))
    _mock_calls(install_llm_sdk, [OpenAIError("boom")])

    await generate_choices(_request(), "*장면*")

    assert {c["provider"] for c in calls} == {"not-deepseek"}


def test_choices_result_requires_an_explicit_provider() -> None:
    """`ChoicesResult.provider`도 기본값을 두지 않는다(KNK-674 리뷰 M3 — LlmUsage와 같은 이유)."""
    with pytest.raises(TypeError):
        chat_choices.ChoicesResult(
            choices=["가", "나", "다"],
            input_tokens=None,
            output_tokens=None,
            retry_count=0,
            model="m",
        )


def test_choices_result_provider_cannot_be_passed_by_position() -> None:
    """provider는 이름으로만 넘긴다(KNK-674 2차 리뷰 4번 — LlmUsage와 같은 이유).

    지금은 맨 끝이라 순서로 넣어도 맞지만, 앞에 칸이 하나 끼는 순간 위치로 넘긴 값들이
    한 칸씩 밀린다. 모델도 공급자도 문자열이라 그 사고는 에러 없이 지나간다.
    """
    with pytest.raises(TypeError):
        chat_choices.ChoicesResult(["가", "나", "다"], 1, 2, 0, "m", "deepseek")

    # 이름을 적으면 그대로 만들어진다.
    res = chat_choices.ChoicesResult(
        choices=["가", "나", "다"],
        input_tokens=1,
        output_tokens=2,
        retry_count=0,
        model="m",
        provider="deepseek",
    )
    assert res.model == "m" and res.provider == "deepseek"
