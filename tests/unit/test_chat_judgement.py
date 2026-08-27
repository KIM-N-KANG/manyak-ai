"""사건·엔딩 판정 서비스 — 스킵·흡수·보정 로직 검증 (KNK-484).

SDK 경계를 모킹해(통로 이관 후의 목 지점 — `install_llm_sdk`) ①재료 없으면 호출 자체가
스킵되고 ②실패는 흡수돼 3필드 null이며 ③목록 밖 이름·형식 위반은 코드가 무효화하는지
(D7 보정) 확인한다.
"""

import asyncio
import json
import time

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


def _mock_call(install, item):
    """단일 판정 호출을 모킹한다. Exception이면 raise, str(json)이면 응답. 호출 수를 센다."""
    state = {"calls": 0}

    async def _create(**kwargs):
        state["calls"] += 1
        if isinstance(item, Exception):
            raise item
        return _Resp(item, usage=_Usage(11, 3))

    install(_create)
    return state


# ── 스킵: 재료 없는 턴(현행 트래픽)은 호출·비용이 0이다 ─────────────────────
async def test_skips_llm_when_no_materials(install_llm_sdk) -> None:
    state = _mock_call(install_llm_sdk, '{"target_main_event": null}')
    res = await generate_judgement(_request(), "*장면*")
    assert state["calls"] == 0
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None
    assert res.input_tokens is None and res.output_tokens is None


# ── 정상 판정 파싱 ──────────────────────────────────────────────────────────
async def test_parses_valid_judgement(install_llm_sdk) -> None:
    _mock_call(
        install_llm_sdk,
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
async def test_llm_failure_absorbed_to_nulls(install_llm_sdk) -> None:
    _mock_call(install_llm_sdk, OpenAIError("boom"))
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None


async def test_invalid_json_absorbed_to_nulls(install_llm_sdk) -> None:
    _mock_call(install_llm_sdk, "이건 JSON이 아님")
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None and res.ending_name is None


async def test_failure_capture_carries_latency_and_retry(monkeypatch, install_llm_sdk) -> None:
    # AN-4-8 — 실패 캡처에 retry_count(단일 호출이라 0)와 latency_ms가 실린다(KNK-529).
    calls: list = []
    monkeypatch.setattr(chat_judgement, "capture_ai_exception", lambda *a, **k: calls.append(k))
    _mock_call(install_llm_sdk, OpenAIError("boom"))
    await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert len(calls) == 1
    assert calls[0]["retry_count"] == 0
    assert isinstance(calls[0]["latency_ms"], int) and calls[0]["latency_ms"] >= 0
    assert calls[0]["provider"] == "deepseek"  # provider 태그도 함께(KNK-674)


# ── 보정: 목록 밖 이름·형식 위반은 무효화한다(D7) ───────────────────────────
async def test_unknown_names_nullified(install_llm_sdk) -> None:
    _mock_call(
        install_llm_sdk,
        '{"target_main_event": {"name": "없는 사건", "progress_turns": 1},'
        ' "occurred_main_event_name": "없는 사건", "ending_name": "없는 엔딩"}',
    )
    res = await generate_judgement(_request(main_events=_EVENTS, endings=_ENDINGS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None


async def test_negative_progress_turns_nullified(install_llm_sdk) -> None:
    _mock_call(install_llm_sdk, '{"target_main_event": {"name": "반란의 서막", "progress_turns": -3}}')
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None


async def test_already_occurred_event_not_reported_again(install_llm_sdk) -> None:
    _mock_call(install_llm_sdk, '{"occurred_main_event_name": "선왕의 유언"}')
    res = await generate_judgement(
        _request(main_events=_EVENTS, occurred=["선왕의 유언"]), "*장면*"
    )
    assert res.occurred_main_event_name is None


async def test_target_cleared_when_same_event_occurred(install_llm_sdk) -> None:
    # 완결로 판정된 사건을 계속 목표로 들고 있으면 코드가 목표를 비운다(완결 직후 상태).
    _mock_call(
        install_llm_sdk,
        '{"target_main_event": {"name": "반란의 서막", "progress_turns": 5},'
        ' "occurred_main_event_name": "반란의 서막"}',
    )
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.occurred_main_event_name == "반란의 서막"
    assert res.target_main_event is None


async def test_target_in_prior_occurred_nullified(install_llm_sdk) -> None:
    # 이전 턴들에서 이미 완결된 사건을 목표로 되보고하면 무효화한다(occurred 가드와 대칭, #1).
    _mock_call(install_llm_sdk, '{"target_main_event": {"name": "선왕의 유언", "progress_turns": 2}}')
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
async def test_malformed_response_absorbed_to_nulls(install_llm_sdk, resp) -> None:
    # 응답 껍데기가 깨져도(빈 choices·message 없음) 흡수해 3필드 null로 돌아간다 —
    # gather로 전파돼 completed 없이 턴이 깨지지 않게 한다.
    # 통로 이관(KNK-673) 후 경로가 바뀌었다: 어댑터가 그런 응답을 빈 본문으로 정규화하고
    # 호출부의 "빈 응답" 판정이 받는다. 결과(판정 null)는 같다.
    async def _create(**kwargs):
        return resp

    install_llm_sdk(_create)
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None


# ── 호출 인자 계약 단언 (KNK-584 재감사 #8) ───────────────────────────────────
# 가짜가 kwargs를 버리면 model·json 모드·max_tokens·thinking 회귀를 못 잡는다.
# 넘긴 인자를 붙잡아 판정 호출 계약(선택지와 같은 비스트리밍 json 단발)을 고정한다.
async def test_judgement_call_contract(install_llm_sdk) -> None:
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    await generate_judgement(_request(main_events=_EVENTS, endings=_ENDINGS), "*장면*")

    assert captured["model"] == chat_judgement.settings.chat_model
    assert [m["role"] for m in captured["messages"]] == ["system", "user"]
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["max_tokens"] == chat_judgement._MAX_TOKENS
    # 추론 끄기는 이제 호출부가 아니라 등록부(use_thinking=False)의 뜻을 어댑터가 옮긴 것이다.
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    # 타임아웃을 호출마다 넘긴다 — 비우면 상한이 SDK 기본값(10분)으로 늘어난다(KNK-673).
    assert captured["timeout"] == chat_judgement._TIMEOUT_SECONDS == 60.0
    assert "stream" not in captured  # 판정은 비스트리밍 단발 호출


def test_build_user_removes_character_image_syntax_from_output() -> None:
    ai_output = (
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 기다렸어?\n"
        "미라: 나도 왔어."
    )

    user = chat_judgement._build_user(_request(main_events=_EVENTS), ai_output)

    assert "[[" not in user
    assert "https://cdn.example.com/serin.webp" not in user
    assert "세린: 기다렸어?" in user
    assert "미라: 나도 왔어." in user


# ── 비-dict 응답도 흡수한다 (KNK-673 리뷰) ───────────────────────────────────
# 선택지에는 같은 검사가 있는데 판정에는 없어 "JSON 객체가 아님" 분기가 한 번도 실행되지
# 않았다. 이 분기가 죽으면 배열 응답이 _sanitize로 흘러 들어가 턴을 깨뜨린다.
async def test_non_object_json_absorbed_to_nulls(monkeypatch, install_llm_sdk) -> None:
    captures: list[dict] = []
    monkeypatch.setattr(
        chat_judgement, "capture_ai_exception", lambda *args, **kwargs: captures.append(kwargs)
    )
    _mock_call(install_llm_sdk, '["반란의 서막"]')  # 유효 JSON이지만 객체가 아님
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    assert res.target_main_event is None
    assert res.occurred_main_event_name is None
    assert res.ending_name is None
    assert captures[0]["error_code"] == "invalid_ai_response"


# ── 토큰 누락은 0이 아니라 null (KNK-673 리뷰) ───────────────────────────────
# 성공 경로의 가짜가 늘 usage를 채워 주면 "null을 0으로 바꾸는" 회귀를 못 잡는다
# (변이 `result_llm.usage.input_tokens or 0`이 통과했다). 없는 경우를 따로 태운다.
async def test_missing_usage_stays_null(install_llm_sdk) -> None:
    async def _create(**kwargs):
        return _Resp('{"target_main_event": null}', usage=None)  # usage 없는 응답

    install_llm_sdk(_create)
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")

    assert res.input_tokens is None and res.output_tokens is None  # 0이 아니라 null


# ── 전송 오류와 내용물 오류의 구분 (KNK-673 리뷰) ────────────────────────────
# 결과 모양(3필드 null)만 보면 둘이 구분되지 않아, 깨진 JSON을 공급자 장애로 잘못 접어도
# 테스트가 통과했다. 그러면 Sentry 오류 이름이 조용히 바뀌어 원인 추적이 헛돈다.
async def test_broken_json_is_reported_as_invalid_ai_response(monkeypatch, install_llm_sdk) -> None:
    from src.core.sentry import ERROR_INVALID_AI_RESPONSE, classify_error_code

    captured: list = []
    monkeypatch.setattr(chat_judgement, "capture_ai_exception", lambda e, **k: captured.append(e))
    _mock_call(install_llm_sdk, "이건 JSON이 아님")

    await generate_judgement(_request(main_events=_EVENTS), "*장면*")

    assert len(captured) == 1
    assert isinstance(captured[0], json.JSONDecodeError)
    assert classify_error_code(captured[0]) == ERROR_INVALID_AI_RESPONSE


async def test_our_own_bug_is_not_absorbed_into_nulls(monkeypatch, install_llm_sdk) -> None:
    """우리 코드의 결함은 판정 null로 덮지 않고 그대로 올려보낸다.

    흡수 대상은 전송 오류와 내용물 오류뿐이다. 예외 절을 넓히면 오타·형 실수까지 "판정이
    안 나왔네"로 위장돼, 사건 진행이 조용히 멈춘 원인을 영영 못 찾는다.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("우리 코드의 결함")

    monkeypatch.setattr(chat_judgement, "_sanitize", _boom)
    _mock_call(install_llm_sdk, '{"target_main_event": null}')

    with pytest.raises(RuntimeError):
        await generate_judgement(_request(main_events=_EVENTS), "*장면*")


# ── provider는 고정값이 아니라 지금 쓰는 모델의 공급자다 (KNK-674 리뷰 H2) ────
async def test_failure_capture_provider_follows_the_selected_model(
    monkeypatch, other_provider_model, install_llm_sdk
) -> None:
    """판정 실패 태그도 지금 쓰는 모델의 공급자를 가리킨다 — 상수면 구분되지 않는다."""
    other_provider_model(chat_judgement)
    calls: list = []
    monkeypatch.setattr(chat_judgement, "capture_ai_exception", lambda *a, **k: calls.append(k))
    _mock_call(install_llm_sdk, OpenAIError("boom"))

    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")

    assert res.target_main_event is None  # 실패는 여전히 흡수된다
    assert calls[0]["provider"] == "not-deepseek"


async def test_provider_is_resolved_before_the_call_not_inside_the_handler(
    monkeypatch, install_llm_sdk
) -> None:
    """provider 조회는 LLM 호출 **전에** 한다 — 실패하면 헛돈이 안 나가야 한다(KNK-674 리뷰 L1).

    조회가 호출 전에 있는지를 **LLM을 한 번도 부르지 않았다**로 확인한다 — try 안으로
    돌아가면 LLM을 먼저 부른 뒤에야 이 자리에 닿으므로 호출 수가 0이 아니게 된다.

    아래 `pytest.raises`가 말하듯 이 예외는 **여전히 밖으로 샌다.** 위치를 바꿔도 흡수되지
    않는다(LlmConfigError는 LlmError가 아니다) — 그 경로는 기동 검사가 막는 몫이다.
    """
    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        raise OpenAIError("boom")

    install_llm_sdk(_create)

    def _boom(_model: str) -> str:
        raise RuntimeError("등록부 조회 실패")

    monkeypatch.setattr(chat_judgement.llm, "provider_of", _boom)

    with pytest.raises(RuntimeError):
        await generate_judgement(_request(main_events=_EVENTS), "*장면*")

    assert calls["n"] == 0  # LLM을 부르기도 전에 막힌다


# ── 전체 시간 상한 (KNK-749 회귀) ─────────────────────────────────────────────
# `_TIMEOUT_SECONDS`를 SDK에만 넘기면 그것은 **시도 하나의 상한**이다. 시간 초과도 재시도
# 대상이라 SDK가 총 3회까지 부르고(`openai_sdk._MAX_RETRIES = 2`) 실제 대기가 세 배로
# 늘어난다. 그러면 백엔드의 SSE 전체 상한(120초)을 넘겨 판정만 늦는 게 아니라 턴이 통째로
# 실패한다. wait_for로 전체를 묶었는지 고정한다 — 안쪽 호출이 아무리 길어도 예산에서 끊고,
# 그 호출을 취소하며(남은 재시도도 함께 멈춘다), 결과는 판정 null로 흡수한다.
async def test_total_timeout_bounds_slow_call(monkeypatch, install_llm_sdk) -> None:
    captures: list[dict] = []
    monkeypatch.setattr(
        chat_judgement,
        "capture_ai_exception",
        lambda exc, **k: captures.append({"exc": exc, **k}),
    )
    monkeypatch.setattr(chat_judgement, "_TIMEOUT_SECONDS", 0.05)
    state = {"called": False, "cancelled": False}

    async def _create(**kwargs):
        state["called"] = True
        try:
            await asyncio.sleep(5)  # 예산의 100배 — 묶여 있지 않으면 여기서 5초를 기다린다
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    began = time.monotonic()
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*")
    elapsed = time.monotonic() - began

    assert state["called"], "호출 자체는 나갔어야 한다(스킵이 아니라 시간 초과 경로)"
    # 묶여 있지 않으면 안쪽 sleep(5초)을 그대로 기다린다. 기준을 2초로 둬도 5초와 뚜렷이
    # 갈리므로, 도커·CI가 잠깐 밀려도 흔들리지 않는다.
    assert elapsed < 2.0, f"전체 상한이 안 걸렸다 — {elapsed:.2f}초 기다림"
    assert state["cancelled"], "시간이 다 되면 안쪽 호출을 취소해야 남은 재시도가 멈춘다"
    # 이 요청에는 진행 중이던 목표가 없다 — 그래서 3필드 전부 null이 맞다. 목표가 있는
    # 요청은 되돌려 보내야 하며, 그쪽은 아래 test_timeout_keeps_the_target...이 본다.
    assert (res.target_main_event, res.occurred_main_event_name, res.ending_name) == (
        None,
        None,
        None,
    )
    # "무슨 예외든 잡혔다"가 아니라 **시간 초과로** 끝났는지까지 못 박는다. 보고도 딱 한 번이어야
    # 한다 — 흡수 경로가 두 번 돌면 같은 실패가 Sentry에 겹쳐 쌓인다.
    assert len(captures) == 1, f"Sentry 보고는 한 번이어야 한다 — {len(captures)}회"
    assert isinstance(captures[0]["exc"], TimeoutError), captures[0]["exc"]
    assert captures[0]["error_code"] == "provider_timeout"


# 시간이 다 돼 끊긴 턴도 **진행 중이던 목표는 그대로 되돌려 보낸다**. 이 상한은 우리가 건
# 것이라(본문이 오래 걸린 턴에는 판정에 몇 초만 준다), 그 몇 초를 넘겼다고 사용자가 쌓아온
# 사건 진행을 지우는 것은 앞뒤가 안 맞는다. 안 부른 턴과 결과가 같아야 한다.
async def test_timeout_keeps_the_target_the_request_carried(monkeypatch, install_llm_sdk) -> None:
    monkeypatch.setattr(chat_judgement, "capture_ai_exception", lambda *a, **k: None)
    state = {"called": False}

    async def _create(**kwargs):
        state["called"] = True
        await asyncio.sleep(5)  # 예산보다 훨씬 오래 — 시간 초과로 끊긴다
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    req = _request(
        main_events=_EVENTS,
        target=TargetMainEvent(name="선왕의 유언", progress_turns=4),
    )
    # 예산이 **양수**다 — 스킵 분기가 아니라 호출 후 시간 초과 경로를 타야 한다.
    res = await generate_judgement(req, "*장면*", budget_seconds=0.05)

    assert state["called"], "호출은 나갔어야 한다(스킵이 아니라 시간 초과 경로)"
    assert res.target_main_event is not None, "목표가 null로 나가면 백엔드가 진행을 지운다"
    assert res.target_main_event.name == "선왕의 유언"
    assert res.target_main_event.progress_turns == 4, "판정이 없었으니 카운터는 동결한다"
    assert (res.occurred_main_event_name, res.ending_name) == (None, None)


# 시간 초과가 아닌 실패(빈 응답·깨진 JSON·전송 오류)는 종전대로 3필드 null이다.
# 이번 티켓 앞에도 있던 문제라 백엔드 가드와 함께 따로 다룬다 — 범위를 넓히지 않았다는 고정.
async def test_other_failures_still_return_null(install_llm_sdk) -> None:
    async def _create(**kwargs):
        return _Resp("이건 JSON이 아니다", usage=_Usage(11, 3))

    install_llm_sdk(_create)
    req = _request(
        main_events=_EVENTS,
        target=TargetMainEvent(name="선왕의 유언", progress_turns=4),
    )
    res = await generate_judgement(req, "*장면*")

    assert res.target_main_event is None, "시간 초과가 아닌 실패까지 되돌리면 범위가 넓어진다"


# ── 남은 시간에 맞춰 예산을 줄인다 (KNK-750 회귀) ─────────────────────────────
# 호출부가 넘긴 남은 시간이 상수보다 짧으면 그쪽을 쓴다. 안 그러면 본문이 오래 걸린 턴에서
# 판정이 턴 전체 상한을 넘겨 턴을 죽인다.
async def test_budget_is_capped_by_what_the_caller_has_left(install_llm_sdk) -> None:
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    await generate_judgement(_request(main_events=_EVENTS), "*장면*", budget_seconds=12.0)

    assert captured["timeout"] == 12.0


# 반대로 남은 시간이 넉넉해도 상수를 넘기지는 않는다 — 둘 중 작은 값이다.
async def test_budget_never_exceeds_the_constant(install_llm_sdk) -> None:
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    await generate_judgement(_request(main_events=_EVENTS), "*장면*", budget_seconds=9999.0)

    assert captured["timeout"] == chat_judgement._TIMEOUT_SECONDS == 60.0


# 남은 시간이 없으면 아예 부르지 않는다 — 결과를 받아도 실을 자리가 없고, 부르는 만큼
# completed만 더 늦어져 턴이 죽을 확률만 올라간다.
async def test_no_call_when_nothing_is_left(install_llm_sdk) -> None:
    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    res = await generate_judgement(_request(main_events=_EVENTS), "*장면*", budget_seconds=0.0)

    assert calls["n"] == 0, "남은 시간이 없으면 LLM을 부르면 안 된다"
    assert (res.target_main_event, res.occurred_main_event_name, res.ending_name) == (
        None,
        None,
        None,
    )


# 남은 시간이 없어 판정을 건너뛰더라도 **진행 중이던 목표는 그대로 되돌려 보낸다**.
# null로 보내면 백엔드가 그것을 목표 해제로 읽어 사용자가 쌓아온 진행을 지운다
# (`ChatTurnPersister.applyMainEventState`). 판정을 못 돌렸을 뿐인데 상태가 바뀌면 안 된다.
async def test_skipping_judgement_keeps_the_target_the_request_carried(install_llm_sdk) -> None:
    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        return _Resp('{"target_main_event": null}', usage=_Usage(11, 3))

    install_llm_sdk(_create)
    req = _request(
        main_events=_EVENTS,
        target=TargetMainEvent(name="선왕의 유언", progress_turns=4),
    )
    res = await generate_judgement(req, "*장면*", budget_seconds=0.0)

    assert calls["n"] == 0, "남은 시간이 없으면 LLM을 부르면 안 된다"
    assert res.target_main_event is not None, "목표가 null로 나가면 백엔드가 진행을 지운다"
    assert res.target_main_event.name == "선왕의 유언"
    assert res.target_main_event.progress_turns == 4, "판정이 없었으니 카운터는 동결한다"
    # 이번 턴에 무슨 일이 있었는지는 판정만 알 수 있다 — 안 돌렸으니 지어내지 않는다.
    assert (res.occurred_main_event_name, res.ending_name) == (None, None)
