from pydantic import BaseModel


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
