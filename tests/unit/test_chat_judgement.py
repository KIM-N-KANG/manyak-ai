"""사건·엔딩 판정 서비스 — 스킵·흡수·보정 로직 검증 (KNK-484).

LLM(_client)을 모킹해 ①재료 없으면 호출 자체가 스킵되고 ②실패는 흡수돼 3필드
null이며 ③목록 밖 이름·형식 위반은 코드가 무효화하는지(D7 보정) 확인한다.
"""

import pytest
from openai import OpenAIError

from src.schemas.chat_turn import (
    ChatStartSettings,
    ChatStorySettings,
    ChatTurnRequest,
    EndingCandidate,
    MainEvent,
    TargetMainEvent,
)
from src.services import chat_judgement
from src.services.chat_judgement import generate_judgement


@pytest.fixture(autouse=True)
def _silence_sentry(monkeypatch):
    # 실패 경로가 Sentry를 부르지 않도록 noop으로 막는다(단위 테스트 격리).
    monkeypatch.setattr(chat_judgement, "capture_ai_exception", lambda *a, **k: None)


_EVENTS = [
    MainEvent(name="반란의 서막", description="귀족 연합이 왕좌를 노린다.", key_sentence="반란의 증거를 손에 넣는다."),
    MainEvent(name="선왕의 유언", description="숨겨진 유언장이 드러난다.", key_sentence="유언장의 행방을 쫓는다."),
]
_ENDINGS = [
    EndingCandidate(name="왕좌를 되찾다", achievement_condition="반란군을 규합해 왕좌를 되찾는다.", epilogue="대관식."),
]


def _request(
    main_events: list[MainEvent] | None = None,
    endings: list[EndingCandidate] | None = None,
    target: TargetMainEvent | None = None,
    occurred: list[str] | None = None,
) -> ChatTurnRequest:
    return ChatTurnRequest(
        genre="판타지",
        story_settings=ChatStorySettings(
            world_setting="# 세계관\n아르덴 왕국.",
            character_setting="# 등장인물\n## 레이",
            user_role_setting="# 주인공\n카이",
            rule_setting="# 전개 규칙",
        ),
        start_settings=ChatStartSettings(name="밤", prologue="깊은 밤.", start_situation="레이가 들어선다."),
        history=[],
        user_input="증거를 내민다",
        summary="",
        main_events=main_events or [],
        endings=endings or [],
        target_main_event=target,
        occurred_main_event_names=occurred or [],
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


def _mock_call(monkeypatch, item):
    """단일 판정 호출을 모킹한다. Exception이면 raise, str(json)이면 응답. 호출 수를 센다."""
    state = {"calls": 0}

    async def _create(**kwargs):
        state["calls"] += 1
        if isinstance(item, Exception):
            raise item
        return _Resp(item, usage=_Usage(11, 3))

    monkeypatch.setattr(chat_judgement._client.chat.completions, "create", _create)
    return state


# ── 스킵: 재료 없는 턴(현행 트래픽)은 호출·비용이 0이다 ─────────────────────
async def test_skips_llm_when_no_materials(monkeypatch) -> None:
    state = _mock_call(monkeypatch, '{"target_main_event": null}')
    res = await generate_judgement(_request(), "*장면*")
    assert state["calls"] == 0
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None
    assert res.input_tokens is None and res.output_tokens is None


# ── 정상 판정 파싱 ──────────────────────────────────────────────────────────
async def test_parses_valid_judgement(monkeypatch) -> None:
    _mock_call(
        monkeypatch,
        '{"target_main_event": {"name": "반란의 서막", "progress_turns": 2},'
        ' "occurred_main_event_name": "선왕의 유언", "ending_name": "왕좌를 되찾다"}',
    )
    res = await generate_judgement(_request(main_events=_EVENTS, endings=_ENDINGS), "*장면*")
    assert res.target_main_event is not None
    assert res.target_main_event.name == "반란의 서막"
    assert res.target_main_event.progress_turns == 2
    assert res.occurred_main_event_name == "선왕의 유언"
    assert res.ending_name == "왕좌를 되찾다"
    assert res.input_tokens == 11 and res.output_tokens == 3


# ── 실패 흡수: 판정 실패가 턴을 깨지 않는다 ─────────────────────────────────
async def test_llm_failure_absorbed_to_nulls(monkeypatch) -> None:
    _mock_call(monkeypatch, OpenAIError("boom"))
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None


async def test_invalid_json_absorbed_to_nulls(monkeypatch) -> None:
    _mock_call(monkeypatch, "이건 JSON이 아님")
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None and res.ending_name is None


# ── 보정: 목록 밖 이름·형식 위반은 무효화한다(D7) ───────────────────────────
async def test_unknown_names_nullified(monkeypatch) -> None:
    _mock_call(
        monkeypatch,
        '{"target_main_event": {"name": "없는 사건", "progress_turns": 1},'
        ' "occurred_main_event_name": "없는 사건", "ending_name": "없는 엔딩"}',
    )
    res = await generate_judgement(_request(main_events=_EVENTS, endings=_ENDINGS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None


async def test_negative_progress_turns_nullified(monkeypatch) -> None:
    _mock_call(monkeypatch, '{"target_main_event": {"name": "반란의 서막", "progress_turns": -3}}')
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None


async def test_already_occurred_event_not_reported_again(monkeypatch) -> None:
    _mock_call(monkeypatch, '{"occurred_main_event_name": "선왕의 유언"}')
    res = await generate_judgement(
        _request(main_events=_EVENTS, occurred=["선왕의 유언"]), "*장면*"
    )
    assert res.occurred_main_event_name is None


async def test_target_cleared_when_same_event_occurred(monkeypatch) -> None:
    # 완결로 판정된 사건을 계속 목표로 들고 있으면 코드가 목표를 비운다(완결 직후 상태).
    _mock_call(
        monkeypatch,
        '{"target_main_event": {"name": "반란의 서막", "progress_turns": 5},'
        ' "occurred_main_event_name": "반란의 서막"}',
    )
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.occurred_main_event_name == "반란의 서막"
    assert res.target_main_event is None


async def test_target_in_prior_occurred_nullified(monkeypatch) -> None:
    # 이전 턴들에서 이미 완결된 사건을 목표로 되보고하면 무효화한다(occurred 가드와 대칭, #1).
    _mock_call(monkeypatch, '{"target_main_event": {"name": "선왕의 유언", "progress_turns": 2}}')
    res = await generate_judgement(
        _request(main_events=_EVENTS, occurred=["선왕의 유언"]), "*장면*"
    )
    assert res.target_main_event is None


# ── 흡수: 응답 형태 이상(빈 choices·message None)도 턴을 깨지 않는다(F2) ────────
class _NoChoicesResp:
    choices: list = []
    model = "deepseek-v4-flash"
    usage = None


class _NoneMsgChoice:
    message = None


class _NoneMsgResp:
    choices = [_NoneMsgChoice()]
    model = "deepseek-v4-flash"
    usage = None


@pytest.mark.parametrize("resp", [_NoChoicesResp(), _NoneMsgResp()])
async def test_malformed_response_absorbed_to_nulls(monkeypatch, resp) -> None:
    # choices[0].message.content 접근에서 IndexError(빈 choices)·AttributeError(message None)가
    # 나도 흡수해 3필드 null로 돌아간다 — gather로 전파돼 completed 없이 턴이 깨지지 않게 한다.
    async def _create(**kwargs):
        return resp

    monkeypatch.setattr(chat_judgement._client.chat.completions, "create", _create)
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None
