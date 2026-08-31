import pytest

from src.schemas.story_compile import ThumbnailImageOut
from src.services import story_llm


@pytest.fixture(autouse=True)
def _no_image_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """일반 단위 테스트에서는 실제 이미지 API를 호출하지 않는다(KNK-940·KNK-1047)."""

    async def fake_images(characters, genre_tags):
        return []

    async def fake_thumbnail(characters, genre_tags):
        return ThumbnailImageOut(image_name="썸네일_기본", error="generation_failed")

    monkeypatch.setattr(story_llm, "_generate_character_images_safe", fake_images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", fake_thumbnail)
