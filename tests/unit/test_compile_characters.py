"""컴파일 인물 단위 입력 반영(KNK-837) 검증.

프롬프트가 인물 블록으로 렌더되는지, 사용자가 이름 지은 주변 인물이 인물 카드에서
빠지면 카드 블록만 부분 재호출(refill)로 다시 받는지 고정한다. 전체 재호출이 아니라
refill을 쓰는 이유는 컴파일이 가장 비싼 호출이라서다. 카드 내용 품질은 실측 몫이다.
"""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.schemas.story import CharacterInput
from src.schemas.story_compile import StoryCompileRequest, ThumbnailImageOut
from src.services import story_llm
from src.services.llm.base import PROVIDER_GOOGLE
from src.services.prompt import (
    COMPILE_GEMINI_VERSION,
    build_compile_prompt,
    _COMPILE_GEMINI_SYSTEM,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _spec() -> dict:
    return json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8"))


def _cards(*names: str) -> dict:
    return {"prompt_settings": {"character_setting": [{"name": n} for n in names]}}


def _request(supporting: list[dict]) -> StoryCompileRequest:
    return StoryCompileRequest(
        selected_storyline="x",
        genre_tags=["무협"],
        protagonist={"features": ["신중한"]},
        supporting_characters=supporting,
    )


# ── 프롬프트 렌더링 ──────────────────────────────────────────────────────────
def test_compile_prompt_renders_character_blocks() -> None:
    _, user, _ = build_compile_prompt(
        "라인",
        "정보",
        ["다크 판타지"],
        CharacterInput(name="카일", gender="MALE", features=["신중한"]),
        [CharacterInput(name="로한", gender="MALE", features=["충직한"]), CharacterInput()],
    )
    assert "{{" not in user
    assert "이름: 카일 / 성별: 남성 / 특징: 신중한" in user
    assert "[input_character_id: input-1] 이름: 로한 / 성별: 남성 / 특징: 충직한" in user
    assert "[input_character_id: input-2] 이름: (미정) / 성별: (미정) / 특징: (미정)" in user


# ── 인물 카드 누락 판정 ─────────────────────────────────────────────────────
def test_cards_pass_with_exact_or_suffixed_name() -> None:
    # 카드 name에 호칭이 붙어도(예: "서린 아씨") 포함이면 있는 것으로 본다.
    assert not story_llm._missing_required_characters(
        _cards("서린 아씨", "로한"), required_names=("서린", "로한")
    )


def test_cards_missing_name_detected() -> None:
    assert story_llm._missing_required_characters(_cards("낯선 자"), required_names=("서린",))


def test_cards_noop_without_required_names() -> None:
    # 이름 지은 인물이 없으면 카드가 어떻게 생겼든 판정하지 않는다(빈 블록은 기존 refill 몫).
    assert not story_llm._missing_required_characters({"prompt_settings": {}}, required_names=())


def test_cards_non_string_name_counts_as_missing() -> None:
    # 카드 이름이 배열·null이면 str() 흔적("['서린']")으로 오탐하지 않고 누락으로 판정한다.
    data = {"prompt_settings": {"character_setting": [{"name": ["서린"]}, {"name": None}]}}
    assert story_llm._missing_required_characters(data, required_names=("서린",))


def test_cards_malformed_prompt_settings_no_crash() -> None:
    # LLM이 prompt_settings를 객체가 아닌 값으로 줘도 500으로 새지 않고 "누락"으로 판정한다.
    assert story_llm._missing_required_characters({"prompt_settings": "엉터리"}, required_names=("서린",))


# ── refill 배선 ─────────────────────────────────────────────────────────────
async def test_compile_story_refills_when_input_character_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사용자 인물 표시가 없으면 카드 블록 refill을 2회 시도한 뒤 502로 막는다."""
    calls: list[tuple[str, str]] = []
    spec = _spec()
    spec["prompt_settings"]["character_setting"][0].pop("input_character_id")

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append((str(kwargs.get("label", "compile")), user))
        return json.loads(json.dumps(spec)), story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    with pytest.raises(HTTPException) as ei:
        await story_llm.compile_story(_request([{"name": "서린"}]))

    assert ei.value.status_code == 502
    assert [label for label, _ in calls] == ["compile", "refill#1", "refill#2"]
    # refill 요청이 카드 블록을 다시 채우라고 지목한다.
    assert "character_setting" in calls[1][1]


async def test_compile_story_overwrites_changed_input_character_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM이 사용자 인물 이름을 바꿔도 내부 표시로 찾아 입력 이름을 복원한다."""
    calls: list[str] = []
    spec = _spec()
    spec["prompt_settings"]["character_setting"][0]["name"] = "제니"
    spec["prompt_settings"]["character_setting"][1]["name"] = "라온"

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append(str(kwargs.get("label", "compile")))
        return spec, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    res = await story_llm.compile_story(_request([{"name": "세린"}]))
    assert calls == ["compile"]  # refill 없이 한 번에 통과
    assert "세린" in res.story_settings.character_setting
    assert "제니" not in res.story_settings.character_setting
    assert res.character_appearances[0].name == "세린"
    assert "input_character_id" not in spec["prompt_settings"]["character_setting"][0]


async def test_compile_story_repairs_blocks_names_and_appearance_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """서로 다른 문제를 한 재호출에 담고, 요청한 인물 필드만 원본에 합친다."""
    initial = _spec()
    original_start = dict(initial["start"])
    cards = initial["prompt_settings"]["character_setting"]
    original_personality = cards[0]["personality"]
    initial["start"]["prologue"] = ""
    cards[0]["hair"] = "   "
    cards[1]["name"] = cards[0]["name"]
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            return initial, story_llm.LlmUsage("m", 1, 1, provider="deepseek")
        return {
            "start": original_start,
            "character_updates": [
                {"index": 0, "hair": "짧은 흑발", "personality": "바뀌면 안 됨"},
                {"index": 1, "name": "라온"},
            ],
        }, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request([]))

    assert len(calls) == 2
    assert "start" in calls[1]
    assert "index 0: hair" in calls[1]
    assert "index 1: name" in calls[1]
    assert res.story_start_settings.prologue == original_start["prologue"]
    assert res.character_appearances[0].hair == "짧은 흑발"
    assert res.character_appearances[1].name == "라온"
    assert original_personality in res.story_settings.character_setting


async def test_compile_story_refills_whole_character_block_before_field_repairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카드 필수 내용이 비면 같은 카드의 index 기반 필드 수정은 함께 요청하지 않는다."""
    initial = _spec()
    initial["prompt_settings"]["character_setting"][0]["tone"] = ""
    initial["prompt_settings"]["character_setting"][0]["hair"] = ""
    valid_cards = _spec()["prompt_settings"]["character_setting"]
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            return initial, story_llm.LlmUsage("m", 1, 1, provider="deepseek")
        return {"character_setting": valid_cards}, story_llm.LlmUsage(
            "m", 1, 1, provider="deepseek"
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request([]))

    assert len(calls) == 2
    assert "character_setting" in calls[1]
    assert "character_updates" not in calls[1]
    assert res.character_appearances[0].hair == valid_cards[0]["hair"]


async def test_compile_story_502_when_duplicate_name_remains_after_refills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _spec()
    duplicate = initial["prompt_settings"]["character_setting"][0]["name"]
    initial["prompt_settings"]["character_setting"][1]["name"] = duplicate
    calls = 0

    async def fake_complete(system: str, user: str, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return initial, story_llm.LlmUsage("m", 1, 1, provider="deepseek")
        return {"character_updates": [{"index": 1, "name": duplicate}]}, story_llm.LlmUsage(
            "m", 1, 1, provider="deepseek"
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    with pytest.raises(HTTPException) as exc:
        await story_llm.compile_story(_request([]))

    assert exc.value.status_code == 502
    assert calls == 3


async def test_compile_story_keeps_success_when_appearance_remains_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _spec()
    initial["prompt_settings"]["character_setting"][0]["hair"] = None
    calls = 0

    async def fake_complete(system: str, user: str, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return initial, story_llm.LlmUsage("m", 1, 1, provider="deepseek")
        return {"character_updates": [{"index": 0, "hair": "\n"}]}, story_llm.LlmUsage(
            "m", 1, 1, provider="deepseek"
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request([]))

    assert calls == 3
    assert res.meta.retry_count == 2
    assert res.character_appearances[0].hair == ""


# ── Gemini 통합 경로(KNK-958) ──────────────────────────────────────────────
async def test_compile_story_gemini_uses_gemini_system_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini 모델이면 첫 호출·응답 meta 모두 Gemini 프롬프트·버전을 쓴다."""
    captured_systems: list[str] = []
    captured_versions: list[dict] = []

    async def fake_complete(system: str, user: str, **kwargs: object):
        captured_systems.append(system)
        captured_versions.append(kwargs.get("prompt_versions", {}))
        return _spec(), story_llm.LlmUsage("gemini-3.6-flash", 1, 1, provider="google")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    # provider_of가 "google"을 반환하도록 — 실제 registry를 타지 않고 직접 패치
    import src.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "provider_of", lambda model: PROVIDER_GOOGLE)

    res = await story_llm.compile_story(_request([{"name": "레이"}]))

    # 첫 호출에 Gemini system prompt가 갔는지
    assert captured_systems[0] == _COMPILE_GEMINI_SYSTEM
    # prompt_versions 키가 COMPILE_GEMINI인지
    assert "COMPILE_GEMINI" in captured_versions[0]
    assert captured_versions[0]["COMPILE_GEMINI"] == COMPILE_GEMINI_VERSION
    # 응답 meta에도 COMPILE_GEMINI 키가 실리는지
    assert "COMPILE_GEMINI" in res.meta.prompt_versions


async def test_compile_story_gemini_refill_uses_gemini_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini 모델에서 refill이 돌 때도 Gemini system prompt를 쓴다."""
    captured_systems: list[str] = []
    captured_versions: list[dict] = []
    spec = _spec()
    spec["prompt_settings"]["character_setting"][0].pop("input_character_id")

    async def fake_complete(system: str, user: str, **kwargs: object):
        captured_systems.append(system)
        captured_versions.append(kwargs.get("prompt_versions", {}))
        return json.loads(json.dumps(spec)), story_llm.LlmUsage(
            "gemini-3.6-flash", 1, 1, provider="google"
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    import src.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "provider_of", lambda model: PROVIDER_GOOGLE)

    # 사용자 인물 표시가 없으므로 refill이 돈다.
    with pytest.raises(HTTPException) as ei:
        await story_llm.compile_story(_request([{"name": "서린"}]))

    assert ei.value.status_code == 502
    # 첫 호출 + refill 2회 = 3회
    assert len(captured_systems) == 3
    # refill에도 Gemini system prompt가 갔는지
    assert all(s == _COMPILE_GEMINI_SYSTEM for s in captured_systems)
    # refill의 prompt_versions도 COMPILE_GEMINI인지
    assert all("COMPILE_GEMINI" in v for v in captured_versions)


# ── 외형 필드(KNK-937) ───────────────────────────────────────────────────────

_APPEARANCE_FIELDS = ("age", "body", "face", "hair", "outfit", "visual_identity")


def test_appearance_fields_in_valid_spec() -> None:
    """픽스처의 인물 카드가 외형 6필드를 모두 갖고 있다."""
    spec = _spec()
    for card in spec["prompt_settings"]["character_setting"]:
        for field in _APPEARANCE_FIELDS:
            assert field in card and card[field], f"{card['name']}의 {field}가 비어 있다"


def test_appearance_fields_parsed_in_schema() -> None:
    """StorySpec 파싱 시 외형 필드가 CharacterSetting에 제대로 들어간다."""
    from src.schemas.story_compile import StorySpec

    spec = StorySpec(**_spec())
    for c in spec.prompt_settings.character_setting:
        assert c.age
        assert c.body
        assert c.face
        assert c.hair
        assert c.outfit
        assert c.visual_identity


def test_appearance_not_in_togul() -> None:
    """통글 변환 결과에 외형 필드가 포함되지 않는다(이미지 생성 전용)."""
    from src.schemas.story_compile import StorySpec
    from src.services.story_compile_render import spec_to_response

    spec = StorySpec(**_spec())
    response = spec_to_response(spec, thumbnail_image=ThumbnailImageOut(error="generation_failed"))
    togul = response.story_settings.character_setting
    for field in _APPEARANCE_FIELDS:
        # 통글 마크다운에 외형 필드명이 헤더(### age 등)로 등장하면 안 된다
        assert f"### {field}" not in togul


def test_missing_appearance_field_not_blocking() -> None:
    """외형은 필수 블록 누락이 아니라 인물 필드 재호출 대상으로 잡는다."""
    spec = _spec()
    for card in spec["prompt_settings"]["character_setting"]:
        for field in _APPEARANCE_FIELDS:
            card.pop(field, None)
    missing = story_llm._find_missing_keys(spec)
    # 외형 필드 경로가 누락 목록에 없어야 한다
    appearance_missing = [p for p in missing if any(f in p for f in _APPEARANCE_FIELDS)]
    assert appearance_missing == []
    repairs = story_llm._find_character_field_repairs(spec)
    assert repairs == {i: _APPEARANCE_FIELDS for i in range(3)}


def test_character_field_repairs_detects_blank_and_duplicate_names() -> None:
    spec = _spec()
    cards = spec["prompt_settings"]["character_setting"]
    cards[0]["name"] = " \n "
    cards[2]["name"] = cards[1]["name"]

    repairs = story_llm._find_character_field_repairs(spec)

    assert repairs[0] == ("name",)
    assert repairs[2] == ("name",)


def test_duplicate_name_repairs_generated_card_not_input_card() -> None:
    """생성 인물이 사용자 인물 이름과 겹치면 생성 인물의 이름만 다시 받는다."""
    spec = _spec()
    cards = spec["prompt_settings"]["character_setting"]
    cards[0].pop("input_character_id")
    cards[0]["name"] = "세린"
    cards[1]["input_character_id"] = "input-1"
    cards[1]["name"] = "세린"

    protected = story_llm._input_character_indexes(spec, input_count=1)
    repairs = story_llm._find_character_field_repairs(spec, protected)

    assert protected == {1}
    assert repairs == {0: ("name",)}


def test_character_field_repairs_treats_whitespace_appearance_as_empty() -> None:
    spec = _spec()
    card = spec["prompt_settings"]["character_setting"][1]
    card["hair"] = "   "
    card["outfit"] = "\n"

    repairs = story_llm._find_character_field_repairs(spec)

    assert repairs == {1: ("hair", "outfit")}


def test_merge_character_field_repairs_changes_only_requested_fields() -> None:
    spec = _spec()
    original_personality = spec["prompt_settings"]["character_setting"][1]["personality"]
    requested = {1: ("name", "hair")}
    refill = {
        "character_updates": [{
            "index": 1,
            "name": "라온",
            "hair": "짧은 은빛 곱슬머리",
            "personality": "요청하지 않은 변경",
        }]
    }

    story_llm._merge_character_field_repairs(spec, refill, requested)

    card = spec["prompt_settings"]["character_setting"][1]
    assert card["name"] == "라온"
    assert card["hair"] == "짧은 은빛 곱슬머리"
    assert card["personality"] == original_personality


def test_appearance_fields_default_empty() -> None:
    """외형 필드 없이도 StorySpec 파싱이 성공한다(기본값 빈 문자열)."""
    from src.schemas.story_compile import StorySpec

    spec = _spec()
    for card in spec["prompt_settings"]["character_setting"]:
        for field in _APPEARANCE_FIELDS:
            card.pop(field, None)
    parsed = StorySpec(**spec)
    # 파싱 성공하고 외형은 빈 문자열
    for c in parsed.prompt_settings.character_setting:
        assert c.age == ""
        assert c.body == ""
