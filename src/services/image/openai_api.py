"""OpenAI Images API 어댑터(KNK-938).

gpt-image-2 계열 모델을 OpenAI SDK로 호출한다. 하는 일은 셋이다.

1. ImageRequest를 OpenAI images.generate 인자로 옮긴다.
2. 응답에서 이미지 바이너리를 꺼내 ImageResult로 만든다.
3. SDK 예외를 공급자 중립 예외로 접는다.
"""

import base64
import hashlib
import logging

import httpx
from openai import (
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    OpenAIError,
    RateLimitError,
)

from src.services.image.base import (
    PROVIDER_OPENAI,
    ImageBadRequest,
    ImageGenerationError,
    ImageRateLimited,
    ImageRequest,
    ImageResult,
    ImageTimeout,
)

logger = logging.getLogger(__name__)

# 전송 실패 시 SDK 재시도 횟수. 텍스트 LLM과 같은 값(openai_sdk.py 참조).
_MAX_RETRIES = 2

# 공급자별 클라이언트 캐시. 텍스트 LLM과 같은 패턴(openai_sdk.py 참조).
_clients: dict[str, AsyncOpenAI] = {}


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _client(api_key: str, base_url: str | None) -> AsyncOpenAI:
    """OpenAI 클라이언트를 얻는다(없으면 만들어 캐시)."""
    from src.services.image.base import ImageGenerationError

    if not api_key or not api_key.strip():
        raise ImageGenerationError("이미지 생성에 필요한 OpenAI API 키가 설정되지 않았습니다.")
    cache_key = f"{_fingerprint(api_key)}:{base_url or 'default'}"
    if cache_key not in _clients:
        _clients[cache_key] = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=_MAX_RETRIES,
        )
    return _clients[cache_key]


async def generate(req: ImageRequest) -> ImageResult:
    """OpenAI Images API로 이미지를 생성한다.

    output_format="png"로 요청하면 응답의 data[0].b64_json에 base64 인코딩된
    PNG가 담긴다. gpt-image-2는 output_format으로 "b64_json"을 받지 않고
    "png"/"webp"/"jpeg"만 받되, 결과는 b64_json 필드에 base64로 준다.
    """
    from src.core.config import settings

    client = _client(settings.openai_api_key, settings.openai_api_url)
    try:
        response = await client.images.generate(
            model=req.model,
            prompt=req.prompt,
            n=1,
            size=req.size,
            quality=req.quality,
            output_format="png",
            timeout=httpx.Timeout(req.timeout, connect=10.0),
        )
    except APITimeoutError as exc:
        raise ImageTimeout(f"이미지 생성 시간 초과 ({req.timeout}초): {exc}") from exc
    except RateLimitError as exc:
        raise ImageRateLimited(f"이미지 생성 속도 제한: {exc}") from exc
    except BadRequestError as exc:
        raise ImageBadRequest(f"이미지 생성 요청 거부: {exc}") from exc
    except OpenAIError as exc:
        raise ImageGenerationError(f"이미지 생성 실패: {exc}") from exc

    b64_data = response.data[0].b64_json
    if not b64_data:
        raise ImageGenerationError("이미지 응답에 데이터가 없습니다.")

    return ImageResult(
        image_bytes=base64.b64decode(b64_data),
        model=req.model,
        provider=PROVIDER_OPENAI,
    )
