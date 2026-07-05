import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.schemas.story_compile import (
    StoryCompileRequest,
    StoryCompileResponse,
    StorySpec,
)
from src.services import story_llm
from src.services.prompt import build_compile_prompt
from src.services.story_compile_render import spec_to_response

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _request() -> StoryCompileRequest:
    return StoryCompileRequest(
        selected_storyline="x",
        additional_info="",
        genre_tags=["다크 판타지", "느와르"],
        protagonist_tags=["신중한"],
        supporting_tags=["거친"],
    )


# ── 스키마 ──────────────────────────────────────────────────────────────────
def test_valid_spec_parses() -> None:
    spec = StorySpec(**_load("spec_valid.json"))
    assert spec.meta.genre == "다크 판타지"
    assert 1 <= len(spec.prompt_settings.character_setting) <= 5
    assert spec.prompt_settings.plot_setting.premise
    assert spec.prompt_settings.user_role_setting.name
    assert len(spec.suggested_inputs) == 3


def test_too_many_characters_rejected() -> None:
    # 인물 6명 — 상한 5 초과로 거부(suggested_inputs는 정상 3개라 거부 사유는 인물 수뿐)
    with pytest.raises(ValidationError):
        StorySpec(**_load("spec_chars_6.json"))


def test_five_characters_allowed() -> None:
    # 상한이 5명이므로 5명은 통과해야 한다.
    data = _load("spec_valid.json")
    base = data["prompt_settings"]["character_setting"][0]
    data["prompt_settings"]["character_setting"] = [
        dict(base, name=f"인물{i}") for i in range(5)
    ]
    spec = StorySpec(**data)
    assert len(spec.prompt_settings.character_setting) == 5


# ── 프롬프트 ────────────────────────────────────────────────────────────────
def test_strip_code_fence() -> None:
    fenced = '```json\n{"a": 1}\n```'
    assert story_llm._strip_code_fence(fenced) == '{"a": 1}'
    # 펜스가 없으면 그대로
    assert story_llm._strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_build_compile_prompt_substitutes_all_slots() -> None:
    system, user = build_compile_prompt(
        "스토리라인 본문",
        "추가정보 본문",
        ["다크 판타지"],
        ["신중한"],
        ["충직한", "거친"],
    )
    assert system  # SYSTEM 블록 비어있지 않음
    assert "{{" not in user  # 자리표시자 잔류 없음
    assert "스토리라인 본문" in user
    assert "추가정보 본문" in user
    assert "다크 판타지" in user
    assert "충직한, 거친" in user


def test_build_compile_prompt_empty_additional_info() -> None:
    _, user = build_compile_prompt("라인", "", ["판타지"], ["용감한"], ["거친"])
    assert "{{" not in user
    assert "(없음)" in user


# ── genre 주입 ──────────────────────────────────────────────────────────────
def test_inject_genre_overwrites() -> None:
    data = {"meta": {"genre": "WRONG"}}
    story_llm._inject_genre(data, ["다크 판타지", "느와르"])
    assert data["meta"]["genre"] == "다크 판타지, 느와르"


# ── 빈 필수키 검증 ──────────────────────────────────────────────────────────
def test_find_missing_keys_clean() -> None:
    assert story_llm._find_missing_keys(_load("spec_valid.json")) == []


def test_find_missing_keys_allows_empty_preference_and_genre() -> None:
    data = _load("spec_valid.json")
    data["prompt_settings"]["user_role_setting"]["preference"] = ""
    data["meta"]["genre"] = ""
    # preference(선택)·genre(코드가 덮어씀)는 빈 값이어도 통과
    assert story_llm._find_missing_keys(data) == []


def test_find_missing_keys_detects_empty_required() -> None:
    data = _load("spec_valid.json")
    data["prompt_settings"]["world_setting"] = "   "  # 공백만
    data["start"]["prologue"] = ""
    data["prompt_settings"]["character_setting"][1]["tone"] = ""
    missing = story_llm._find_missing_keys(data)
    assert "prompt_settings.world_setting" in missing
    assert "start.prologue" in missing
    assert "prompt_settings.character_setting[1].tone" in missing


def test_find_missing_keys_requires_exactly_three_inputs() -> None:
    data = _load("spec_valid.json")
    data["suggested_inputs"] = ["하나", "둘"]  # 2개
    assert "suggested_inputs" in story_llm._find_missing_keys(data)


def test_find_missing_keys_tolerates_wrong_types() -> None:
    # LLM이 객체 자리에 문자열·문자열 배열을 줘도 500이 아니라 missing으로 수집해야 한다.
    data = _load("spec_valid.json")
    data["meta"] = "문자열로 잘못 옴"
    data["prompt_settings"]["character_setting"] = ["레이", "세린"]
    missing = story_llm._find_missing_keys(data)
    assert "meta.title" in missing
    assert "prompt_settings.character_setting[0].name" in missing


def test_block_of_maps_paths() -> None:
    assert story_llm._block_of("meta.title") == "meta"
    assert story_llm._block_of("prompt_settings.world_setting") == "world_setting"
    assert story_llm._block_of("prompt_settings.plot_setting.premise") == "plot_setting"
    assert story_llm._block_of("prompt_settings.character_setting[1].tone") == "character_setting"
    assert story_llm._block_of("start.prologue") == "start"
    assert story_llm._block_of("suggested_inputs[0]") == "suggested_inputs"


# ── 통글 변환 ───────────────────────────────────────────────────────────────
def test_spec_to_response_renders_nested_markdown() -> None:
    spec = StorySpec(**_load("spec_valid.json"))
    res = spec_to_response(spec)

    assert isinstance(res, StoryCompileResponse)
    # stories: 값 그대로 + genre 제외
    assert res.stories.title == "잿빛 왕관"
    assert not hasattr(res.stories, "genre")
    # story_settings: 통글 마크다운 + 레이어 분배(plot/tone/length 흡수)
    assert res.story_settings.world_setting.startswith("# 세계관")
    assert "# 전제" in res.story_settings.world_setting
    assert "# 갈등" in res.story_settings.world_setting
    assert "## 레이" in res.story_settings.character_setting
    assert "### 말투" in res.story_settings.character_setting
    assert "# 문체 톤" in res.story_settings.rule_setting
    assert "# 분량 배분" in res.story_settings.rule_setting
    # 시작 설정·추천 입력은 값 그대로
    assert res.story_start_settings.name == "선왕의 장례식 날"
    assert len(res.story_suggested_inputs) == 3


# ── compile_story 통합 ──────────────────────────────────────────────────────
async def test_compile_story_returns_nested_response(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_complete(system: str, user: str, **_kwargs: object):
        # 완전한 결과 → 재호출 없음. (dict, 사용 메타) 튜플 반환.
        return _load("spec_valid.json"), story_llm.LlmUsage("deepseek-test", 100, 200)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert isinstance(res, StoryCompileResponse)
    assert res.stories.title == "잿빛 왕관"
    assert "## 레이" in res.story_settings.character_setting
    assert len(res.story_suggested_inputs) == 3
    # 로깅 메타(KNK-243): model=응답값, prompt_versions=객체, retry_count=0(재호출 없음)
    assert res.meta is not None
    assert res.meta.model == "deepseek-test"
    assert res.meta.provider == "deepseek"
    assert list(res.meta.prompt_versions) == ["COMPILE"]
    assert res.meta.prompt_versions["COMPILE"] >= 1
    assert res.meta.input_token_count == 100
    assert res.meta.output_token_count == 200
    assert res.meta.retry_count == 0


async def test_compile_story_refills_missing_block(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            data = _load("spec_valid.json")
            data["prompt_settings"]["world_setting"] = ""  # 1차: 빈 필드
            return data, story_llm.LlmUsage("m", 100, 200)
        return {"world_setting": "복구된 세계관 설정"}, story_llm.LlmUsage("m", 10, 20)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert len(calls) == 2  # 최초 1 + 부분 재호출 1
    assert "복구된 세계관 설정" in res.story_settings.world_setting
    # retry_count=재호출 횟수, 토큰은 본호출+재호출 합산
    assert res.meta.retry_count == 1
    assert res.meta.input_token_count == 110  # 100 + 10
    assert res.meta.output_token_count == 220  # 200 + 20


async def test_compile_story_502_after_max_refill(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        data["start"]["prologue"] = ""  # 매번 빈 채 → 재호출로도 못 채움
        return data, story_llm.LlmUsage("m", 1, 1)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    with pytest.raises(HTTPException) as exc:
        await story_llm.compile_story(_request())
    assert exc.value.status_code == 502


# ── Sentry 캡처 경계(KNK-262) — 성공은 조용, 실패만 보고 ──────────────────────
async def test_compile_story_success_does_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 컴파일에서는 Sentry capture를 호출하지 않는다."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _load("spec_valid.json"), story_llm.LlmUsage("deepseek-test", 100, 200)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    calls: list = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda *a, **k: calls.append(1))

    res = await story_llm.compile_story(_request())
    assert res.stories.title  # 성공
    assert calls == []  # 성공 경로 — 미호출


async def test_compile_story_schema_failure_captures(monkeypatch: pytest.MonkeyPatch) -> None:
    """StorySpec 파싱 실패(인물 6명 > 상한 5)는 schema_validation_failed로 캡처한다."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        # 빈 필수키는 없어 재호출 없이 통과하지만, 인물 6명이라 StorySpec 파싱에서 거부된다.
        return _load("spec_chars_6.json"), story_llm.LlmUsage("m", 1, 1)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    calls: list = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda *a, **k: calls.append(k))

    with pytest.raises(HTTPException) as exc:
        await story_llm.compile_story(_request())
    assert exc.value.status_code == 502
    assert len(calls) == 1
    assert calls[0]["error_code"] == "schema_validation_failed"


# ── 엔딩·주요 사건 (KNK-417) ─────────────────────────────────────────────────
def test_spec_to_response_passes_events_and_endings() -> None:
    # 엔딩·사건이 응답에 항목별 이산 필드로 실려야 한다(회귀 방지).
    spec = StorySpec(**_load("spec_valid.json"))
    res = spec_to_response(spec)
    assert 3 <= len(res.story_main_events) <= 5
    ev = res.story_main_events[0]
    assert ev.name and ev.description and ev.key_sentence
    assert sorted(e.ending_type for e in res.story_endings) == ["BAD", "HAPPY", "NORMAL"]
    end = res.story_endings[0]
    assert end.ending_requirement and end.ending_epilogue


def test_one_ending_per_type_rejected() -> None:
    # 엔딩 타입 분포 위반(HAPPY 2 + BAD)은 StorySpec 파싱에서 거부돼야 한다.
    data = _load("spec_valid.json")
    for e, t in zip(data["endings"], ["HAPPY", "HAPPY", "BAD"]):
        e["ending_type"] = t
    with pytest.raises(ValidationError):
        StorySpec(**data)


def test_events_out_of_range_rejected() -> None:
    # 사건은 3~5개. 2개(하한 위반)·6개(상한 위반) 모두 거부.
    for n in (2, 6):
        data = _load("spec_valid.json")
        base = data["main_events"][0]
        data["main_events"] = [dict(base, name=f"사건{i}") for i in range(n)]
        with pytest.raises(ValidationError):
            StorySpec(**data)


def test_find_missing_keys_detects_event_ending_issues() -> None:
    # 사건 개수 하한 위반
    data = _load("spec_valid.json")
    data["main_events"] = data["main_events"][:2]
    assert "main_events" in story_llm._find_missing_keys(data)
    # 사건 항목의 빈 필드
    data = _load("spec_valid.json")
    data["main_events"][0]["key_sentence"] = ""
    assert "main_events[0].key_sentence" in story_llm._find_missing_keys(data)
    # 엔딩 항목의 빈 필드
    data = _load("spec_valid.json")
    data["endings"][1]["ending_epilogue"] = "   "
    assert "endings[1].ending_epilogue" in story_llm._find_missing_keys(data)
    # 엔딩 타입 분포 위반은 재호출 대상(endings 블록)으로 잡혀야 한다
    data = _load("spec_valid.json")
    for e, t in zip(data["endings"], ["HAPPY", "HAPPY", "BAD"]):
        e["ending_type"] = t
    assert "endings" in story_llm._find_missing_keys(data)


def test_find_missing_keys_clean_with_events_and_endings() -> None:
    # 정상 spec은 엔딩·사건 때문에 오탐이 나지 않아야 한다.
    assert story_llm._find_missing_keys(_load("spec_valid.json")) == []


def test_normalize_ending_types_uppercases() -> None:
    # 대소문자 흔들림(happy)은 정규화로 흡수하되 분포는 유지.
    data = _load("spec_valid.json")
    for e, t in zip(data["endings"], ["happy", "Normal", "bAd"]):
        e["ending_type"] = t
    story_llm._normalize_ending_types(data)
    assert [e["ending_type"] for e in data["endings"]] == ["HAPPY", "NORMAL", "BAD"]


def test_block_of_maps_event_ending_paths() -> None:
    assert story_llm._block_of("main_events[0].key_sentence") == "main_events"
    assert story_llm._block_of("endings") == "endings"
    assert story_llm._block_of("endings[2].ending_type") == "endings"


async def test_compile_story_refills_bad_ending_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 첫 응답이 분포 위반(HAPPY 2 + BAD)이면 endings만 재호출로 구제해야 한다.
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            data = _load("spec_valid.json")
            for e, t in zip(data["endings"], ["HAPPY", "HAPPY", "BAD"]):
                e["ending_type"] = t
            return data, story_llm.LlmUsage("m", 100, 200)
        return {"endings": _load("spec_valid.json")["endings"]}, story_llm.LlmUsage("m", 10, 20)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert len(calls) == 2  # 최초 1 + 분포 위반 재호출 1
    assert sorted(e.ending_type for e in res.story_endings) == ["BAD", "HAPPY", "NORMAL"]


async def test_compile_story_normalizes_lowercase_ending_type_without_refill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 소문자 ending_type은 정규화로 흡수돼 재호출 없이 통과해야 한다.
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append(user)
        data = _load("spec_valid.json")
        for e, t in zip(data["endings"], ["happy", "normal", "bad"]):
            e["ending_type"] = t
        return data, story_llm.LlmUsage("m", 100, 200)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert len(calls) == 1  # 재호출 없음
    assert sorted(e.ending_type for e in res.story_endings) == ["BAD", "HAPPY", "NORMAL"]
