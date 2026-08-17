"""인물 단위 입력 스키마(KNK-833) 검증.

계약(5-ai-server.md §5-3-2): 주인공·주변 인물은 {name, gender, features[]} 세트이고
세 항목 전부 선택이다. 빈 값은 LLM 자동 생성 대상이므로 스키마가 거부하면 안 되고,
gender는 "MALE"·"FEMALE"·null 외의 값을 거부해야 한다. 개수 상한(5명·특징 3개)은
백엔드 소관이라 여기서 검증하지 않는 것도 계약이다.
"""

import pytest
from pydantic import ValidationError

from src.schemas.story import CharacterInput, StorylinesRequest


def test_character_input_all_fields_optional() -> None:
    c = CharacterInput()
    assert c.name is None
    assert c.gender is None
    assert c.features == []


def test_character_input_rejects_unknown_gender() -> None:
    with pytest.raises(ValidationError):
        CharacterInput(gender="남")


def test_name_normalized_and_blank_treated_as_missing() -> None:
    # 뒤 공백은 다듬고(NFC·trim), 공백뿐인 이름은 미입력(None)과 같다.
    assert CharacterInput(name="서린 ").name == "서린"
    assert CharacterInput(name="   ").name is None
    # 분해형 한글(NFD)도 조합형(NFC)으로 통일된다 — 등장 검증 문자열 대조의 전제.
    import unicodedata

    nfd = unicodedata.normalize("NFD", "서린")
    assert CharacterInput(name=nfd).name == "서린"
    # 안쪽 개행·연속 공백도 한 칸으로 — 등장 검증이 절대 못 맞추는 입력을 만들지 않는다.
    assert CharacterInput(name="서\n린").name == "서 린"
    assert CharacterInput(name="김  도형").name == "김 도형"


def test_blank_features_dropped() -> None:
    # 빈 문자열·공백 항목은 버려져 "특징: , ," 렌더를 막고, 전부 비면 (미정) 경로를 탄다.
    assert CharacterInput(features=["", "  ", "거친 "]).features == ["거친"]
    assert CharacterInput(features=["", " "]).features == []


def test_non_string_feature_rejected() -> None:
    # 문자열이 아닌 특징은 조용히 버리지 않고 422로 드러낸다(백엔드 형식 버그 노출).
    with pytest.raises(ValidationError):
        CharacterInput(features=[123, "용감한"])


def test_explicit_null_accepted_as_empty() -> None:
    # 백엔드가 "값 없음"을 명시적 null로 보내도 빈 배열과 동일하다(로어북 KNK-422와 같은 관례).
    req = StorylinesRequest(
        genre_tags=["무협"],
        protagonist={"name": None, "gender": None, "features": None},
        supporting_characters=None,
    )
    assert req.protagonist.features == []
    assert req.supporting_characters == []


def test_storylines_request_supporting_defaults_to_empty() -> None:
    # 주변 인물 0명 허용 — supporting_characters를 아예 안 보내도 통과한다.
    req = StorylinesRequest(genre_tags=["무협"], protagonist={"features": ["신중한"]})
    assert req.supporting_characters == []


def test_storylines_request_accepts_empty_character_sets() -> None:
    # 항목을 하나도 안 채운 빈 세트도 인원으로 받는다(빈 세트 = 자동 생성 1명).
    req = StorylinesRequest(
        genre_tags=["무협"],
        protagonist={},
        supporting_characters=[{}, {"name": "서린", "gender": "FEMALE"}],
    )
    assert len(req.supporting_characters) == 2
    assert req.supporting_characters[0].features == []
    assert req.supporting_characters[1].name == "서린"


def test_storylines_request_counts_not_enforced() -> None:
    # 개수 상한(주변 인물 5명·특징 3개)은 백엔드가 강제한다 — AI 서버는 통과시킨다.
    req = StorylinesRequest(
        genre_tags=["무협"],
        protagonist={"features": ["a", "b", "c", "d"]},
        supporting_characters=[{"features": []} for _ in range(6)],
    )
    assert len(req.supporting_characters) == 6
    assert len(req.protagonist.features) == 4


# ── 이름 중복 차단(KNK-841) ──────────────────────────────────────────────────

from src.schemas.story_compile import StoryCompileRequest


def test_duplicate_supporting_names_rejected() -> None:
    # 주변 인물끼리 이름이 겹으면 422.
    with pytest.raises(ValidationError, match="중복"):
        StorylinesRequest(
            genre_tags=["무협"],
            protagonist={},
            supporting_characters=[
                {"name": "서린", "gender": "FEMALE"},
                {"name": "서린", "gender": "MALE"},
            ],
        )


def test_protagonist_name_collides_with_supporting() -> None:
    # 주인공 이름이 주변 인물과 같아도 422.
    with pytest.raises(ValidationError, match="중복"):
        StorylinesRequest(
            genre_tags=["무협"],
            protagonist={"name": "서린"},
            supporting_characters=[{"name": "서린"}],
        )


def test_compile_duplicate_names_rejected() -> None:
    # 컴파일 요청에서도 같은 검증이 동작한다.
    with pytest.raises(ValidationError, match="중복"):
        StoryCompileRequest(
            selected_storyline="x",
            genre_tags=["무협"],
            protagonist={"name": "카일"},
            supporting_characters=[
                {"name": "카일", "gender": "MALE"},
            ],
        )


def test_none_names_do_not_collide() -> None:
    # 이름을 비운 인물끼리는 중복이 아니다 — LLM이 각각 다른 이름을 짓는다.
    req = StorylinesRequest(
        genre_tags=["무협"],
        protagonist={},
        supporting_characters=[{}, {}],
    )
    assert len(req.supporting_characters) == 2


def test_normalized_duplicate_caught() -> None:
    # 공백·NFC 정규화 후 같아지는 이름도 중복으로 잡힌다.
    with pytest.raises(ValidationError, match="중복"):
        StorylinesRequest(
            genre_tags=["무협"],
            protagonist={"name": "서린 "},
            supporting_characters=[{"name": "서린"}],
        )
