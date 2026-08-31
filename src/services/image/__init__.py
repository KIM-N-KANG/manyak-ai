"""이미지 생성 통로 — 모델 이름으로 공급자를 고르는 단일 진입점(KNK-938).

텍스트 LLM 통로(src/services/llm/)와 같은 3층 구조다.
호출부는 "이 프롬프트로 이미지 만들어줘"만 말하고, 어느 회사 SDK로 가는지는 모른다.

- base.py        — 요청·결과·공급자 중립 예외
- openai_api.py  — OpenAI Images API 어댑터
- __init__.py    — 모델 이름 → 어댑터 분기 (이 파일)
"""

import math
import re
from urllib.parse import urlparse

from src.core.config import settings
from src.services.image.base import (
    ADAPTER_OPENAI_IMAGE,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)

__all__ = [
    "generate_image",
    "validate_startup",
    "ImageGenerationError",
    "ImageRequest",
    "ImageResult",
    "THUMBNAIL_IMAGE_SIZE",
]

# 모델 이름 → 어댑터 매핑. 모델이 늘면 여기에 추가한다.
_MODEL_ADAPTERS: dict[str, str] = {
    "gpt-image-2": ADAPTER_OPENAI_IMAGE,
    "gpt-image-2-low": ADAPTER_OPENAI_IMAGE,
    "gpt-image-2-2026-04-21": ADAPTER_OPENAI_IMAGE,
}

_QUALITIES = frozenset({"low", "medium", "high"})
_SIZE_RE = re.compile(r"^[1-9]\d*x[1-9]\d*$")

# 스토리 썸네일(표지) 크기. 기존 프리셋 썸네일과 같은 3:4 세로라 프론트 수정이 없다(KNK-1047).
# 인물 이미지 크기(IMAGE_SIZE)를 뒤집어 쓰지 않고 상수로 고정한다 — 환경변수를 늘리지 않는다.
THUMBNAIL_IMAGE_SIZE = "768x1024"


def _adapter_for(model: str) -> str:
    """모델 이름으로 어댑터를 고른다."""
    adapter = _MODEL_ADAPTERS.get(model)
    if adapter is None:
        raise ImageGenerationError(
            f"이미지 모델 '{model}'은 등록되지 않았습니다. "
            f"등록된 모델: {', '.join(sorted(_MODEL_ADAPTERS))}."
        )
    return adapter


def validate_startup() -> None:
    """이미지 설정 오류를 첫 생성 요청이 아니라 서버 기동에서 드러낸다."""
    try:
        adapter = _adapter_for(settings.image_model)
    except ImageGenerationError as exc:
        raise ImageGenerationError(f"IMAGE_MODEL: {exc}") from exc

    if adapter != ADAPTER_OPENAI_IMAGE:
        raise ImageGenerationError(
            f"IMAGE_MODEL={settings.image_model}의 어댑터 '{adapter}'를 처리할 코드가 없습니다."
        )

    api_key = settings.openai_api_key
    if not api_key.strip():
        raise ImageGenerationError("IMAGE_MODEL은 OPENAI_API_KEY가 필요하지만 값이 비어 있습니다.")
    if "\n" in api_key or "\r" in api_key:
        raise ImageGenerationError("OPENAI_API_KEY에 개행이 있습니다.")
    if api_key != api_key.strip():
        raise ImageGenerationError("OPENAI_API_KEY에 앞뒤 공백이 있습니다.")
    if not api_key.isascii():
        raise ImageGenerationError("OPENAI_API_KEY에 ASCII가 아닌 문자가 있습니다.")
    if not api_key.isprintable() or any(ch.isspace() for ch in api_key):
        raise ImageGenerationError("OPENAI_API_KEY에 공백 또는 제어문자가 있습니다.")

    base_url = settings.openai_api_url
    if base_url is not None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ImageGenerationError(
                "OPENAI_API_URL이 올바른 주소가 아닙니다(http:// 또는 https://와 호스트가 필요)."
            )

    if settings.image_quality not in _QUALITIES:
        raise ImageGenerationError(
            f"IMAGE_QUALITY는 {', '.join(sorted(_QUALITIES))} 중 하나여야 합니다."
        )
    if not _SIZE_RE.fullmatch(settings.image_size):
        raise ImageGenerationError("IMAGE_SIZE는 '가로x세로' 형식의 양의 정수여야 합니다.")
    if not math.isfinite(settings.image_timeout) or settings.image_timeout <= 0:
        raise ImageGenerationError("IMAGE_TIMEOUT은 0보다 큰 유한한 초 단위 숫자여야 합니다.")


async def generate_image(prompt: str, *, size: str | None = None) -> ImageResult:
    """이미지를 생성한다. 모델은 IMAGE_MODEL 환경변수로 결정된다.

    호출부는 이 함수만 부른다. 어떤 공급자를 쓰는지, SDK가 뭔지 모른다.
    size를 주지 않으면 IMAGE_SIZE(인물 이미지 크기)를 쓴다. 썸네일처럼 다른 크기가
    필요한 호출부만 명시한다. 명시한 값은 IMAGE_SIZE와 같은 형식 검사를 거친다 —
    잘못된 값이 공급자까지 갔다가 "거부됨"으로 둔갑하면 코드 실수를 못 알아본다.
    """
    model = settings.image_model
    adapter = _adapter_for(model)

    if size is None:
        size = settings.image_size
    elif not _SIZE_RE.fullmatch(size):
        raise ImageGenerationError(f"이미지 크기 '{size}'는 '가로x세로' 형식의 양의 정수여야 합니다.")

    req = ImageRequest(
        model=model,
        prompt=prompt,
        size=size,
        quality=settings.image_quality,
        timeout=settings.image_timeout,
    )

    if adapter == ADAPTER_OPENAI_IMAGE:
        from src.services.image import openai_api

        return await openai_api.generate(req)

    raise ImageGenerationError(f"어댑터 '{adapter}'를 처리할 코드가 없습니다.")
