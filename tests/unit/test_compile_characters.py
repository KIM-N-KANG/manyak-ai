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
from src.schemas.story_compile import StoryCompileRequest
from src.services import story_llm
from src.services.prompt import build_compile_prompt

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
    _, user = build_compile_prompt(
        "라인",
        "정보",
        ["다크 판타지"],
        CharacterInput(name="카일", gender="MALE", features=["신중한"]),
        [CharacterInput(name="로한", gender="MALE", features=["충직한"]), CharacterInput()],
    )
    assert "{{" not in user
    assert "이름: 카일 / 성별: 남성 / 특징: 신중한" in user
    assert "1) 이름: 로한 / 성별: 남성 / 특징: 충직한" in user
    assert "2) 이름: (미정) / 성별: (미정) / 특징: (미정)" in user


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
async def test_compile_story_refills_when_named_character_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카드에 없는 이름(서린)을 요구하면 카드 블록 refill을 2회 시도한 뒤 502로 막는다."""
    calls: list[tuple[str, str]] = []
    spec = _spec()  # 카드 이름: 레이·세린·칸 — "서린"은 없다("세린"과 다른 이름)

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append((str(kwargs.get("label", "compile")), user))
        return json.loads(json.dumps(spec)), story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    with pytest.raises(HTTPException) as ei:
        await story_llm.compile_story(_request([{"name": "서린"}]))

    assert ei.value.status_code == 502
    assert [label for label, _ in calls] == ["compile", "refill#1", "refill#2"]
    # refill 요청이 카드 블록을 다시 채우라고 지목한다(이름 원문은 경로에 없음 — AN-4-10).
    assert "character_setting" in calls[1][1]


async def test_compile_story_passes_when_named_character_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append(str(kwargs.get("label", "compile")))
        return _spec(), story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    res = await story_llm.compile_story(_request([{"name": "세린"}]))  # 카드에 있는 이름
    assert calls == ["compile"]  # refill 없이 한 번에 통과
    assert "세린" in res.story_settings.character_setting
