"""이미지 생성 통로 — 모델 이름으로 공급자를 고르는 단일 진입점(KNK-938).

텍스트 LLM 통로(src/services/llm/)와 같은 3층 구조다.
호출부는 "이 프롬프트로 이미지 만들어줘"만 말하고, 어느 회사 SDK로 가는지는 모른다.

- base.py        — 요청·결과·공급자 중립 예외
- openai_api.py  — OpenAI Images API 어댑터
- __init__.py    — 모델 이름 → 어댑터 분기 (이 파일)
"""

from src.core.config import settings
from src.services.image.base import (
    ADAPTER_OPENAI_IMAGE,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)

__all__ = ["generate_image", "ImageGenerationError", "ImageRequest", "ImageResult"]

# 모델 이름 → 어댑터 매핑. 모델이 늘면 여기에 추가한다.
_MODEL_ADAPTERS: dict[str, str] = {
    "gpt-image-2": ADAPTER_OPENAI_IMAGE,
    "gpt-image-2-low": ADAPTER_OPENAI_IMAGE,
    "gpt-image-2-2026-04-21": ADAPTER_OPENAI_IMAGE,
}


def _adapter_for(model: str) -> str:
    """모델 이름으로 어댑터를 고른다."""
    adapter = _MODEL_ADAPTERS.get(model)
    if adapter is None:
        raise ImageGenerationError(
            f"이미지 모델 '{model}'은 등록되지 않았습니다. "
            f"등록된 모델: {', '.join(sorted(_MODEL_ADAPTERS))}."
        )
    return adapter


async def generate_image(prompt: str) -> ImageResult:
    """이미지를 생성한다. 모델은 IMAGE_MODEL 환경변수로 결정된다.

    호출부는 이 함수만 부른다. 어떤 공급자를 쓰는지, SDK가 뭔지 모른다.
    """
    model = settings.image_model
    adapter = _adapter_for(model)

    req = ImageRequest(
        model=model,
        prompt=prompt,
        size=settings.image_size,
        quality=settings.image_quality,
        timeout=settings.image_timeout,
    )

    if adapter == ADAPTER_OPENAI_IMAGE:
        from src.services.image import openai_api

        return await openai_api.generate(req)

    raise ImageGenerationError(f"어댑터 '{adapter}'를 처리할 코드가 없습니다.")
