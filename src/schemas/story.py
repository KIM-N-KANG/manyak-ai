import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

from src.schemas.response_meta import StoryResponseMeta


def _none_as_empty(v: object) -> object:
    # 백엔드가 "값 없음"을 명시적 null로 보내도 빈 배열과 동일하게 받는다(로어북 KNK-422와 같은 관례).
    return [] if v is None else v


def _clean_name(v: object) -> object:
    """이름을 NFC 정규화·trim한다. 다듬은 결과가 빈 문자열이면 미입력(None)과 동일하다.

    뒤 공백("서린 ")이나 분해형 한글(macOS 클립보드 경로)은 프롬프트에도 지저분하게 실리고,
    등장 검증(문자열 대조)이 절대 통과하지 못하는 입력이 된다 — 스키마에서 한 번에 다듬는다.
    """
    if isinstance(v, str):
        cleaned = unicodedata.normalize("NFC", v).strip()
        return cleaned or None
    return v


def _clean_features(v: object) -> object:
    """특징 목록의 각 항목을 NFC 정규화·trim하고 빈 항목은 버린다(null은 빈 배열)."""
    if v is None:
        return []
    if isinstance(v, list):
        return [
            unicodedata.normalize("NFC", f).strip()
            for f in v
            if isinstance(f, str) and f.strip()
        ]
    return v


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


class StorylinesRequest(BaseModel):
    genre_tags: list[str]
    protagonist: CharacterInput
    supporting_characters: SupportingCharacters = Field(default_factory=list)


class StoryItem(BaseModel):
    id: int
    storyline: str
    recommended_infos: list[str]


class StorylinesResponse(BaseModel):
    stories: list[StoryItem]
    meta: StoryResponseMeta | None = None  # 로깅 메타(KNK-243). 엔드포인트가 항상 채운다.
