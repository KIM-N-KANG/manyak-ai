"""컴파일 스토리 썸네일(표지) 생성(KNK-1047).

컴파일 LLM이 만든 인물 외형과 장르 태그로 표지 프롬프트를 조립해 이미지 한 장을 만든다.
인물 이미지(generate_characters)와 같은 통로를 쓰되 크기만 세로(THUMBNAIL_IMAGE_SIZE)다.
공급자 실패는 예외 대신 결과로 돌려준다 — 표지는 컴파일의 부가물이라 스토리 사용을 막으면
안 된다. 취소(CancelledError)와 이 밖의 예외는 그대로 올라가고, 호출부(story_llm의 safe 함수)가 접는다.
"""

import logging
import time
from dataclasses import dataclass

from src.core.config import settings
from src.core.sentry import FEATURE_THUMBNAIL_IMAGE, capture_ai_exception
from src.schemas.story_compile import CharacterSetting
from src.services.image import THUMBNAIL_IMAGE_SIZE, ImageGenerationError, generate_image
from src.services.image.base import PROVIDER_OPENAI, ImageResult
from src.services.image.prompt import THUMBNAIL_IMAGE_VERSION, build_thumbnail_prompt

logger = logging.getLogger(__name__)


@dataclass
class ThumbnailImageResult:
    """썸네일 생성 결과. image가 None이면 생성 실패(error에 공급자 원문)."""

    image: ImageResult | None = None
    error: str | None = None


async def generate_thumbnail_image(
    characters: list[CharacterSetting],
    genre_tags: list[str],
) -> ThumbnailImageResult:
    """표지 한 장을 생성한다. 인물 선정은 build_thumbnail_prompt가 한다(외형 완비 앞 1~2명,
    없으면 첫 인물). 인물 이미지의 세마포어 밖에서 돌므로 동시 공급자 호출은 최대 6이다.
    """
    prompt = build_thumbnail_prompt(characters, genre_tags)
    start = time.monotonic()
    try:
        result = await generate_image(prompt, size=THUMBNAIL_IMAGE_SIZE)
        logger.info("썸네일 생성 성공")
        return ThumbnailImageResult(image=result)
    except ImageGenerationError as exc:
        # 실패는 표지만 비우고 컴파일은 살리므로 여기서 Sentry에 보내지 않으면 아무도 모른다.
        # 인물 이름·프롬프트는 싣지 않는다(인물 이미지와 같은 원칙).
        capture_ai_exception(
            exc,
            feature=FEATURE_THUMBNAIL_IMAGE,
            provider=PROVIDER_OPENAI,
            model=settings.image_model,
            prompt_versions={"THUMBNAIL_IMAGE": THUMBNAIL_IMAGE_VERSION},
            retry_count=0,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        logger.warning("썸네일 생성 실패 — %s", exc)
        return ThumbnailImageResult(error=str(exc))
