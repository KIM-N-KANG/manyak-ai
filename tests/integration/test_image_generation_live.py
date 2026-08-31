import os

import pytest

from src.core.config import settings
from src.services.image import THUMBNAIL_IMAGE_SIZE, generate_image
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


async def test_thumbnail_size_live_returns_portrait_webp() -> None:
    """썸네일 크기(768x1024 세로)를 공급자가 받는지 확인한다(KNK-1047).

    거부되면 ImageBadRequest가 나서 실패한다 — 그 경우 크기 상수를 바꿔야 한다.
    """
    result = await generate_image(
        "A quiet fantasy castle courtyard at dusk, anime illustration, no people, no text.",
        size=THUMBNAIL_IMAGE_SIZE,
    )

    assert result.image_bytes[:4] == b"RIFF"
    assert result.image_bytes[8:12] == b"WEBP"
    # 공급자가 크기를 다른 세로 크기로 바꿔 주면 잡히도록 상수와 정확히 비교한다(Codex 리뷰 4).
    width, height = _webp_size(result.image_bytes)
    assert f"{width}x{height}" == THUMBNAIL_IMAGE_SIZE, f"요청 {THUMBNAIL_IMAGE_SIZE} != 응답 {width}x{height}"


def _webp_size(data: bytes) -> tuple[int, int]:
    """WebP 바이너리에서 (가로, 세로)를 읽는다. VP8X·VP8L·VP8 세 형식을 처리한다."""
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8L":
        b = data[21:25]
        w = ((b[1] & 0x3F) << 8 | b[0]) + 1
        h = ((b[3] & 0x0F) << 10 | b[2] << 2 | (b[1] & 0xC0) >> 6) + 1
        return w, h
    if chunk == b"VP8 ":
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return w, h
    raise AssertionError(f"알 수 없는 WebP 청크: {chunk!r}")
