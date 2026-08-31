"""스토리 썸네일(표지) 프롬프트 조립 단위 테스트(KNK-1049).

표지 인물 선정 규칙(외형 완비 인물 중 앞 1~2명)과 인물 0·1·2명 각각의 조립 결과를 검증한다.
실제 이미지 품질은 유닛으로 검증되지 않는다 — 라이브 실측은 별도.
"""

import re

import pytest

from src.schemas.story_compile import CharacterSetting
from src.services.image.prompt import (
    THUMBNAIL_IMAGE_VERSION,
    THUMBNAIL_MAX_CHARACTERS,
    build_thumbnail_prompt,
    has_full_appearance,
    select_thumbnail_characters,
)


# ── 픽스처 ────────────────────────────────────────────────────────────────────

def _char(name: str = "레이", **overrides) -> CharacterSetting:
    defaults = {
        "name": name,
        "gender": "남성",
        "personality": "충직한 원칙주의자.",
        "tone": "직설적인 말투.",
        "motivation": "진실 규명.",
        "attitude_to_user": "신뢰하는 전우.",
        "age": "20대 후반",
        "body": "건장한",
        "face": "각진 턱선, 굳은 표정",
        "hair": "짧은 단발",
        "outfit": "은색 판금 흉갑",
        "visual_identity": "왼쪽 관자놀이의 칼자국",
    }
    defaults.update(overrides)
    return CharacterSetting(**defaults)


_GENRE = ["로맨스 판타지", "학원"]


# ── 외형 완비 판정 ────────────────────────────────────────────────────────────

def test_has_full_appearance_true_when_all_six_filled() -> None:
    assert has_full_appearance(_char())


@pytest.mark.parametrize("field", ["age", "body", "face", "hair", "outfit", "visual_identity"])
def test_has_full_appearance_false_when_any_field_blank(field) -> None:
    """빈 문자열이든 공백만 있든 하나라도 비면 외형 미완."""
    assert not has_full_appearance(_char(**{field: ""}))
    assert not has_full_appearance(_char(**{field: "   "}))


# ── 표지 인물 선정 ────────────────────────────────────────────────────────────

def test_select_falls_back_to_first_card_when_none_complete() -> None:
    """외형이 다 찬 인물이 없어도 카드 첫 번째 인물을 넣는다(표지에 인물 최소 1명)."""
    chars = [_char("A", age=""), _char("B", outfit=" ")]
    assert [x.name for x in select_thumbnail_characters(chars)] == ["A"]


def test_select_returns_empty_only_when_no_cards() -> None:
    assert select_thumbnail_characters([]) == []


def test_select_skips_incomplete_and_keeps_card_order() -> None:
    """외형이 빈 인물은 건너뛰고, 남은 인물은 카드 순서를 유지한다."""
    a, b, c = _char("A", face=""), _char("B"), _char("C")
    assert [x.name for x in select_thumbnail_characters([a, b, c])] == ["B", "C"]


def test_select_caps_at_two_characters() -> None:
    chars = [_char("A"), _char("B"), _char("C"), _char("D")]
    picked = select_thumbnail_characters(chars)
    assert THUMBNAIL_MAX_CHARACTERS == 2
    assert [x.name for x in picked] == ["A", "B"]


# ── 프롬프트 조립 ─────────────────────────────────────────────────────────────

def test_build_thumbnail_prompt_with_two_characters() -> None:
    """인물 두 명의 외형이 각각 <character> 블록으로 들어가고 장르가 채워진다."""
    prompt = build_thumbnail_prompt(
        [_char("A", hair="긴 은발"), _char("B", gender="여성", hair="붉은 포니테일")],
        _GENRE,
    )
    assert "<genre>로맨스 판타지, 학원</genre>" in prompt
    assert prompt.count("<character>") == 2
    assert "<hair>긴 은발</hair>" in prompt
    assert "<hair>붉은 포니테일</hair>" in prompt
    assert "<gender>여성</gender>" in prompt
    assert "{{" not in prompt


def test_build_thumbnail_prompt_partial_character_omits_blank_fields() -> None:
    """외형이 일부 빈 인물이 들어가면 빈 칸은 태그째 빠지고 나머지만 실린다."""
    prompt = build_thumbnail_prompt([_char("A", age="", face="  ")], _GENRE)
    assert prompt.count("<character>") == 1
    assert "<age>" not in prompt
    assert "<face>" not in prompt
    assert "<hair>짧은 단발</hair>" in prompt
    assert "{{" not in prompt


def test_build_thumbnail_prompt_with_no_input_characters() -> None:
    """인물 카드가 아예 없을 때만 `없음`으로 장르 장면 표지를 만든다."""
    prompt = build_thumbnail_prompt([], ["무협"])
    assert "<genre>무협</genre>" in prompt
    assert re.search(r"<characters>\s*없음\s*</characters>", prompt)


def test_build_thumbnail_prompt_excludes_character_name() -> None:
    """인물 이름은 표지 프롬프트에 싣지 않는다(글자를 넣지 않고, 그림에 도움이 안 됨)."""
    prompt = build_thumbnail_prompt([_char("유니크한이름")], _GENRE)
    assert "유니크한이름" not in prompt


def test_build_thumbnail_prompt_keeps_fixed_blocks() -> None:
    """고정 블록(나이 지시·구도·출력)이 템플릿에서 빠지지 않고 들어온다."""
    prompt = build_thumbnail_prompt([_char()], _GENRE)
    for tag in ("<task>", "<character_interpretation>", "<age_direction>", "<composition>", "<output>"):
        assert tag in prompt
    assert prompt.startswith("<image_prompt>")
    assert prompt.endswith("</image_prompt>")


def test_thumbnail_template_version_is_positive_int() -> None:
    assert isinstance(THUMBNAIL_IMAGE_VERSION, int)
    assert THUMBNAIL_IMAGE_VERSION >= 1
