"""스토리라인 인물 단위 입력 반영(KNK-836) 검증.

프롬프트 렌더링(이름·성별·특징, (미정) 표시, 0명 폴백)과, 사용자가 이름 지은
주변 인물이 세 편 모두 등장하는지 보는 invalid 검증을 고정한다. 실제 LLM 출력
품질(이름이 자연스럽게 쓰였는지 등)은 실측 몫이다.
"""

import pytest

from src.schemas.story import CharacterInput
from src.services import story_llm
from src.services.prompt import build_storylines_prompt


def _stories(*storylines: str) -> dict:
    return {
        "stories": [
            {"id": i + 1, "storyline": s, "recommended_infos": ["가", "나", "다"]}
            for i, s in enumerate(storylines)
        ]
    }


# ── 프롬프트 렌더링 ──────────────────────────────────────────────────────────
def test_prompt_renders_character_fields() -> None:
    _, user = build_storylines_prompt(
        ["무협"],
        CharacterInput(name="무영", gender="MALE", features=["신중한", "계획적인"]),
        [CharacterInput(name="서린", gender="FEMALE", features=["다정한"])],
    )
    assert "{{" not in user  # 자리표시자 잔류 없음
    assert "이름: 무영 / 성별: 남성 / 특징: 신중한, 계획적인" in user
    assert "1) 이름: 서린 / 성별: 여성 / 특징: 다정한" in user


def test_prompt_marks_empty_fields_as_undecided() -> None:
    _, user = build_storylines_prompt(["무협"], CharacterInput(), [CharacterInput(features=["거친"])])
    assert "이름: (미정) / 성별: (미정) / 특징: (미정)" in user  # 주인공 전체 미정
    assert "1) 이름: (미정) / 성별: (미정) / 특징: 거친" in user


def test_prompt_zero_supporting_characters_fallback() -> None:
    # 0명이면 주변 인물 구성 전체를 LLM에 맡긴다(0명 허용 계약).
    _, user = build_storylines_prompt(["무협"], CharacterInput(), [])
    assert "(미정 — 이야기에 어울리는 주변 인물을 직접 구성하라)" in user
    assert "{{" not in user


def test_prompt_placeholder_like_name_not_expanded() -> None:
    # 이름에 자리표시자 모양 문자열이 들어와도 문자 그대로 남는다(단일 패스 치환).
    _, user = build_storylines_prompt(
        ["무협"],
        CharacterInput(name="{{주변_인물}}"),
        [CharacterInput(name="서린")],
    )
    assert "이름: {{주변_인물}} / " in user  # 주인공 줄에 문자 그대로
    assert user.count("1) 이름: 서린") == 1  # 주변 인물 블록은 제자리에 한 번만


# ── 인물 등장 검증 ──────────────────────────────────────────────────────────
def test_validate_passes_when_named_characters_appear() -> None:
    data = _stories("서린이 검을 든다.", "골목에서 서린을 만났다.", "서린은 웃지 않았다.")
    story_llm._validate_storylines(data, required_names=("서린",))  # 예외 없음


def test_validate_rejects_when_named_character_missing() -> None:
    # 두 번째 편에만 서린이 빠짐 — invalid로 판정해 재호출 경로를 태운다.
    data = _stories("서린이 검을 든다.", "낯선 사내가 나타났다.", "서린은 웃지 않았다.")
    with pytest.raises(story_llm._InvalidAiResponse):
        story_llm._validate_storylines(data, required_names=("서린",))


def test_validate_normalizes_storyline_before_matching() -> None:
    # LLM 출력이 분해형 한글(NFD)이어도 NFC로 통일해 대조한다 — 형태 차이로 오판하지 않는다.
    import unicodedata

    nfd_story = unicodedata.normalize("NFD", "서린이 검을 든다.")
    data = _stories(nfd_story, "서린을 만났다.", "서린은 웃지 않았다.")
    story_llm._validate_storylines(data, required_names=("서린",))  # 예외 없음


def test_validate_without_required_names_keeps_existing_contract() -> None:
    # 이름 지은 인물이 없으면(전부 미정·0명) 기존 3편 계약 검증만 남는다.
    story_llm._validate_storylines(_stories("가.", "나.", "다."))
