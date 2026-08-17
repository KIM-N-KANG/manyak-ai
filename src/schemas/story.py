import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, model_validator

from src.schemas.response_meta import StoryResponseMeta


def _none_as_empty(v: object) -> object:
    # 백엔드가 "값 없음"을 명시적 null로 보내도 빈 배열과 동일하게 받는다(로어북 KNK-422와 같은 관례).
    return [] if v is None else v


def _clean_name(v: object) -> object:
    """이름을 NFC 정규화하고 앞뒤·안쪽 공백을 정리한다. 결과가 비면 미입력(None)과 동일하다.

    뒤 공백("서린 ")·안쪽 개행·연속 공백·분해형 한글(macOS 클립보드 경로)은 프롬프트에도
    지저분하게 실리고, 등장 검증(문자열 대조)이 절대 통과하지 못하는 입력이 된다 —
    스키마에서 한 번에 다듬는다(안쪽 공백 연속은 한 칸으로).
    """
    if isinstance(v, str):
        cleaned = " ".join(unicodedata.normalize("NFC", v).split())
        return cleaned or None
    return v


def _clean_features(v: object) -> object:
    """특징 목록의 각 문자열 항목을 NFC 정규화·trim하고 빈 항목은 버린다(null은 빈 배열).

    문자열이 아닌 항목은 걸러내지 않고 그대로 둔다 — Pydantic이 422로 거부하게 해서
    백엔드 형식 버그를 조용히 삼키지 않는다(성별 오값 처리와 대칭).
    """
    if v is None:
        return []
    if not isinstance(v, list):
        return v
    cleaned: list[object] = []
    for f in v:
        if isinstance(f, str):
            stripped = unicodedata.normalize("NFC", f).strip()
            if stripped:
                cleaned.append(stripped)
        else:
            cleaned.append(f)
    return cleaned


class CharacterInput(BaseModel):
    """인물 단위 입력 세트(KNK-833) — 주인공·주변 인물 공용. 세 항목 전부 선택.

    빈 값(null·빈 배열)은 LLM이 자동 생성한다. 개수 상한(주변 인물 5명·특징 3개)은
    백엔드가 강제하므로 여기서 검증하지 않는다(5-ai-server.md §5-3-2).
    """

    name: Annotated[str | None, BeforeValidator(_clean_name)] = None
    gender: Literal["MALE", "FEMALE"] | None = None
    features: Annotated[list[str], BeforeValidator(_clean_features)] = Field(default_factory=list)


# 주변 인물 목록 0~5명(0명 허용) — 명시적 null도 "없음"으로 받는다.
SupportingCharacters = Annotated[list[CharacterInput], BeforeValidator(_none_as_empty)]


def _check_duplicate_names(
    protagonist: CharacterInput, supporting_characters: list[CharacterInput],
) -> None:
    """주인공과 주변 인물을 합쳐 이름 중복이 있으면 거부한다(KNK-841).

    이름을 비운 인물(None)은 LLM이 짓는다 — 중복 판정 대상이 아니다.
    _clean_name이 먼저 돌아 NFC 정규화·공백 정리가 끝난 상태에서 비교한다.
    """
    seen: set[str] = set()
    names = [protagonist.name] + [c.name for c in supporting_characters]
    for n in names:
        if n is None:
            continue
        if n in seen:
            raise ValueError(f"인물 이름이 중복됩니다: {n}")
        seen.add(n)


class StorylinesRequest(BaseModel):
    genre_tags: list[str]
    protagonist: CharacterInput
    supporting_characters: SupportingCharacters = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_names(self) -> "StorylinesRequest":
        _check_duplicate_names(self.protagonist, self.supporting_characters)
        return self


class StoryItem(BaseModel):
    id: int
    storyline: str
    recommended_infos: list[str]


class StorylinesResponse(BaseModel):
    stories: list[StoryItem]
    meta: StoryResponseMeta | None = None  # 로깅 메타(KNK-243). 엔드포인트가 항상 채운다.
