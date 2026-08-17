"""스토리라인 인물 단위 입력 반영(KNK-836·KNK-840) 검증.

프롬프트 렌더링(이름·성별·특징, (미정) 표시, 0명 폴백)과, 사용자가 이름 지은
주변 인물이 빠진 편을 골라 그 편만 다시 받는 판정·병합을 고정한다. 실제 LLM 출력
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


# ── 인물 등장 판정(부분 재호출 대상 고르기, KNK-840) ────────────────────────
def test_no_missing_when_named_characters_appear() -> None:
    data = _stories("서린이 검을 든다.", "골목에서 서린을 만났다.", "서린은 웃지 않았다.")
    assert story_llm._missing_name_indexes(data, ("서린",)) == []


def test_only_deficient_story_is_selected() -> None:
    # 두 번째 편에만 서린이 빠짐 — 그 편 하나만 다시 받는다(세 편 전체 재생성 아님).
    data = _stories("서린이 검을 든다.", "낯선 사내가 나타났다.", "서린은 웃지 않았다.")
    assert story_llm._missing_name_indexes(data, ("서린",)) == [1]


def test_missing_detection_normalizes_before_matching() -> None:
    # LLM 출력이 분해형 한글(NFD)이어도 NFC로 통일해 대조한다 — 형태 차이로 오판하지 않는다.
    import unicodedata

    nfd_story = unicodedata.normalize("NFD", "서린이 검을 든다.")
    data = _stories(nfd_story, "서린을 만났다.", "서린은 웃지 않았다.")
    assert story_llm._missing_name_indexes(data, ("서린",)) == []


def test_no_required_names_means_nothing_to_refill() -> None:
    # 이름 지은 인물이 없으면(전부 미정·0명) 판정하지 않는다.
    assert story_llm._missing_name_indexes(_stories("가.", "나.", "다."), ()) == []


def test_contract_validation_ignores_names() -> None:
    # 계약 검증은 응답이 못 쓸 것인지만 본다 — 인물 누락은 여기서 걸리지 않는다.
    story_llm._validate_storylines(_stories("가.", "나.", "다."))


# ── 부분 재호출 병합 ────────────────────────────────────────────────────────
def test_merge_replaces_only_requested_story() -> None:
    data = _stories("1편", "2편", "3편")
    refill = {"stories": [{"id": 2, "storyline": "새 2편", "recommended_infos": ["가", "나", "다"]}]}
    story_llm._merge_storylines(data, refill, [1])
    assert [s["storyline"] for s in data["stories"]] == ["1편", "새 2편", "3편"]


def test_merge_ignores_unrequested_or_out_of_range_ids() -> None:
    # 요청하지 않은 편·범위 밖 id로 잘 나온 편을 덮어쓰지 못하게 막는다.
    data = _stories("1편", "2편", "3편")
    refill = {
        "stories": [
            {"id": 1, "storyline": "덮어쓰면 안 됨", "recommended_infos": ["가", "나", "다"]},
            {"id": 99, "storyline": "범위 밖", "recommended_infos": ["가", "나", "다"]},
            {"id": "2", "storyline": "id가 문자열", "recommended_infos": ["가", "나", "다"]},
        ]
    }
    story_llm._merge_storylines(data, refill, [1])
    assert [s["storyline"] for s in data["stories"]] == ["1편", "2편", "3편"]


def test_two_stories_missing_detected() -> None:
    # 두 편에서 인물이 빠지면 두 번호가 모두 돌아온다.
    data = _stories("서린이 검을 든다.", "낯선 사내가 나타났다.", "또 다른 사내.")
    assert story_llm._missing_name_indexes(data, ("서린",)) == [1, 2]


def test_multiple_names_partial_missing() -> None:
    # 인물 2명 중 한 명만 빠진 편도 잡힌다.
    data = _stories("서린과 강우가 있다.", "서린만 있다.", "서린과 강우가 있다.")
    assert story_llm._missing_name_indexes(data, ("서린", "강우")) == [1]


def test_merge_replaces_two_stories_at_once() -> None:
    data = _stories("1편", "2편", "3편")
    refill = {"stories": [
        {"id": 1, "storyline": "새 1편", "recommended_infos": ["가", "나", "다"]},
        {"id": 3, "storyline": "새 3편", "recommended_infos": ["가", "나", "다"]},
    ]}
    story_llm._merge_storylines(data, refill, [0, 2])
    assert [s["storyline"] for s in data["stories"]] == ["새 1편", "2편", "새 3편"]


def test_merge_survives_malformed_refill() -> None:
    # 재호출 응답이 stories를 안 담거나 배열이 아니어도 500으로 새지 않는다.
    data = _stories("1편", "2편", "3편")
    story_llm._merge_storylines(data, {"stories": "엉터리"}, [1])
    story_llm._merge_storylines(data, {}, [1])
    assert [s["storyline"] for s in data["stories"]] == ["1편", "2편", "3편"]
