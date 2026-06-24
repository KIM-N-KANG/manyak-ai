from pydantic import BaseModel

from src.schemas.response_meta import StoryResponseMeta


class StorylinesRequest(BaseModel):
    genre_tags: list[str]
    protagonist_tags: list[str]
    supporting_tags: list[str]


class StoryItem(BaseModel):
    id: int
    story: str
    recommended_infos: list[str]


class StorylinesResponse(BaseModel):
    stories: list[StoryItem]
    meta: StoryResponseMeta | None = None  # 로깅 메타(KNK-243). 엔드포인트가 항상 채운다.
