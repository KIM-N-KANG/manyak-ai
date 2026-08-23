"""컴파일 인물별 이미지 병렬 생성(KNK-939).

StorySpec의 주변 인물 카드에서 외형 필드를 꺼내 이미지 프롬프트를 조립하고,
여러 인물의 이미지를 동시에 생성한다. 한 인물의 실패가 다른 인물이나
스토리 컴파일을 중단하지 않는다.
"""

import asyncio
import logging
from dataclasses import dataclass

from src.schemas.story_compile import CharacterSetting
from src.services.image import generate_image, ImageGenerationError
from src.services.image.base import ImageResult
from src.services.image.prompt import build_image_prompt

logger = logging.getLogger(__name__)

# 동시 실행 수 제한. 주변 인물 최대 5명을 한 묶음에 돌린다.
_MAX_CONCURRENCY = 5


@dataclass
class CharacterImageResult:
    """인물별 이미지 생성 결과. image가 None이면 생성 실패(해당 인물은 이미지 없음)."""

    name: str
    image: ImageResult | None = None
    error: str | None = None


async def _generate_one(
    character: CharacterSetting,
    genre_tags: list[str],
    semaphore: asyncio.Semaphore,
) -> CharacterImageResult:
    """인물 하나의 이미지를 생성한다. 실패해도 예외를 던지지 않는다."""
    prompt = build_image_prompt(character, genre_tags)
    if prompt is None:
        logger.info("이미지 생성 건너뜀: %s (외형 필드 부족)", character.name)
        return CharacterImageResult(name=character.name, error="외형 필드 부족")

    async with semaphore:
        try:
            result = await generate_image(prompt)
            logger.info("이미지 생성 성공: %s", character.name)
            return CharacterImageResult(name=character.name, image=result)
        except ImageGenerationError as exc:
            logger.warning("이미지 생성 실패: %s — %s", character.name, exc)
            return CharacterImageResult(name=character.name, error=str(exc))


async def generate_character_images(
    characters: list[CharacterSetting],
    genre_tags: list[str],
) -> list[CharacterImageResult]:
    """주변 인물 전원의 이미지를 병렬 생성한다.

    반환 순서는 입력 characters 순서와 같다.
    실패한 인물은 image=None, error=사유로 돌아온다.
    """
    if not characters:
        return []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    tasks = [asyncio.create_task(_generate_one(c, genre_tags, semaphore)) for c in characters]
    try:
        return list(await asyncio.gather(*tasks))
    except Exception:
        # 예상치 못한 예외 발생 시 아직 돌고 있는 나머지 작업을 취소한다.
        # 취소하지 않으면 응답은 이미 갔는데 이미지 호출만 계속 돌아 비용이 낭비된다.
        for t in tasks:
            if not t.done():
                t.cancel()
        raise
