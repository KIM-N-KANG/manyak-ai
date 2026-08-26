import os

import pytest

from src.core.config import settings
from src.services.image import generate_image
from src.services.image.base import PROVIDER_OPENAI, ImageResult


@pytest.fixture(autouse=True)
def require_live_env() -> None:
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("RUN_LIVE_TESTS=1이 아니면 라이브 통합 테스트를 건너뜁니다")


async def test_image_generation_live_returns_webp() -> None:
    result = await generate_image(
        "A fictional adult fantasy detective, neutral studio background, "
        "fully clothed, non-violent character portrait."
    )

    assert isinstance(result, ImageResult)
    assert result.model == settings.image_model
    assert result.provider == PROVIDER_OPENAI
    assert result.image_bytes[:4] == b"RIFF"
    assert result.image_bytes[8:12] == b"WEBP"
