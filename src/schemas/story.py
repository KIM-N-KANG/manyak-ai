from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

from src.schemas.response_meta import StoryResponseMeta


def _none_as_empty(v: object) -> object:
    # 백엔드가 "값 없음"을 명시적 null로 보내도 빈 배열과 동일하게 받는다(로어북 KNK-422와 같은 관례).
    return [] if v is None else v


class CharacterInput(BaseModel):
    """인물 단위 입력 세트(KNK-833) — 주인공·주변 인물 공용. 세 항목 전부 선택.

    빈 값(null·빈 배열)은 LLM이 자동 생성한다. 개수 상한(주변 인물 5명·특징 3개)은
    백엔드가 강제하므로 여기서 검증하지 않는다(5-ai-server.md §5-3-2).
    """

    name: str | None = None
    gender: Literal["MALE", "FEMALE"] | None = None
    features: Annotated[list[str], BeforeValidator(_none_as_empty)] = Field(default_factory=list)


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
