"""부모 다운로드 → 대화 기반 편집 → 저장용 자식 이미지 결과(KNK-1265).

채팅 이벤트 연결과 부모 대체 표시는 호출부의 후속 작업이다.
"""

import base64
from dataclasses import dataclass
from uuid import uuid4

import httpx

from src.core.config import settings
from src.services.image import generate_image
from src.services.image.base import (
    IMAGE_PURPOSE_CHILD,
    ImageBadRequest,
    ImageGenerationError,
    ImageRateLimited,
    ImageReference,
    ImageTimeout,
)
from src.services.image.child_input import ChildImageInput
from src.services.image.child_prompt import build_child_image_prompt

# 편집 API의 입력 파일 상한. 스트리밍으로 읽어 초과 다운로드를 중단한다.
_MAX_PARENT_BYTES = 50_000_000


@dataclass(frozen=True)
class ChildImageResult:
    name: str
    image_name: str
    image_base64: str | None = None
    content_type: str = "image/webp"
    error: str | None = None


def _reference(data: bytes) -> ImageReference:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        extension, mime = "png", "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        extension, mime = "jpg", "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        extension, mime = "webp", "image/webp"
    else:
        raise ImageGenerationError("부모 이미지 형식이 지원되지 않습니다.")
    return ImageReference(data, mime, f"parent.{extension}")


async def _download_parent(url: str) -> ImageReference:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ImageGenerationError("부모 이미지 URL이 잘못됐습니다.") from exc
    if (
        parsed.scheme != "https"
        or parsed.host not in settings.image_parent_allowed_hosts
        or parsed.port not in (None, 443)
        or parsed.userinfo
    ):
        raise ImageGenerationError("허용되지 않은 부모 이미지 주소입니다.")
    try:
        # 리다이렉트로 내부 주소에 접근하지 못하게 한다. URL·본문은 로그에 남기지 않는다.
        async with httpx.AsyncClient(timeout=settings.image_timeout, follow_redirects=False) as client:
            async with client.stream("GET", parsed) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) >= _MAX_PARENT_BYTES:
                        raise ImageGenerationError("부모 이미지 크기 제한을 초과했습니다.")
        return _reference(bytes(data))
    except httpx.TimeoutException as exc:
        raise ImageTimeout("부모 이미지 다운로드 시간 초과") from exc
    except httpx.HTTPError as exc:
        raise ImageGenerationError("부모 이미지 다운로드 실패") from exc


async def generate_child_image(inputs: ChildImageInput) -> ChildImageResult:
    """한 번 편집하고 결과를 반환한다. 취소는 삼키지 않고 호출부에 전파한다."""
    name = inputs.parent_image.name
    image_name = f"{name}_실시간_{uuid4()}"
    try:
        prompt = build_child_image_prompt(inputs)
        parent = await _download_parent(inputs.parent_image.image_url)
        result = await generate_image(prompt, purpose=IMAGE_PURPOSE_CHILD, reference=parent)
        if not result.image_bytes:
            raise ImageGenerationError("자식 이미지 데이터가 없습니다.")
        return ChildImageResult(
            name=name, image_name=image_name,
            image_base64=base64.b64encode(result.image_bytes).decode("ascii"),
        )
    except ImageTimeout:
        error = "timeout"
    except ImageRateLimited:
        error = "rate_limited"
    except ImageBadRequest:
        error = "rejected"
    except ImageGenerationError:
        error = "generation_failed"
    return ChildImageResult(name=name, image_name=image_name, error=error)
