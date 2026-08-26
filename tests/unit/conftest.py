import pytest

from src.services import story_llm


@pytest.fixture(autouse=True)
def _no_image_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """일반 단위 테스트에서는 실제 이미지 API를 호출하지 않는다(KNK-940)."""

    async def fake_images(characters, genre_tags):
        return []

    monkeypatch.setattr(story_llm, "_generate_character_images_safe", fake_images)
