"""검수할 이미지를 내려받아 모델에 넣을 조각으로 만든다(KNK-1359).

백엔드는 게시물의 표지·인물 이미지를 **공개 서빙 URL**로 준다. 모델에 URL을 그대로 넘기지 않고
서버가 내려받아 data URL로 넣는 이유는 둘이다 — 접근 실패를 우리가 잡아 계약의 오류 코드로
돌려줘야 하고, 내려받은 실제 바이트로 형식·크기를 확인해야 "URL만 보고 승인"이 안 된다.

실패는 두 가지로 나눈다(하네스 Spec §5-9-6). 백엔드가 "다시 시도할 문제"와 "사용자가 이미지를
바꿔야 할 문제"를 구분해야 하기 때문이다.

- `IMAGE_DOWNLOAD_FAILED` — 접근 불가·시간 초과·허용되지 않은 주소. 인프라 문제.
- `IMAGE_INVALID` — 이미지가 아닌 파일·지원하지 않는 형식·장당 상한 초과. 사용자가 고칠 문제.

둘 다 모델을 부르기 전에 확정되므로 실패한 게시물에는 모델 비용이 들지 않는다.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from src.core.config import settings
from src.services.llm.base import ContentPart, image_part
from src.services.moderation.limits import MAX_REQUEST_BYTES

logger = logging.getLogger(__name__)

# 계약의 `error_code` 값. 판정 서비스가 응답에 그대로 싣는다.
ERROR_IMAGE_DOWNLOAD_FAILED = "IMAGE_DOWNLOAD_FAILED"
ERROR_IMAGE_INVALID = "IMAGE_INVALID"

# 지원 형식 — 두 검수 모델(OpenAI·DeepSeek)이 모두 받는 네 가지. 확장자·Content-Type 헤더가
# 아니라 실제 바이트의 머리로 판별한다(부모 이미지 다운로드와 같은 규칙).
_CHUNK_SIZE = 64 * 1024
_MAX_CONCURRENT_DOWNLOADS = 5


class ModerationImageError(Exception):
    """이미지 한 장의 준비 실패. `path`는 실패한 입력 필드 경로, `code`는 계약의 오류 코드.

    메시지에 URL을 넣지 않는다 — 예외 문자열은 로그·Sentry로 흘러가는데, 서빙 URL은 게시물을
    특정할 수 있는 값이라 남기지 않는다.
    """

    code: str = ""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


class ImageDownloadFailed(ModerationImageError):
    code = ERROR_IMAGE_DOWNLOAD_FAILED


class ImageInvalid(ModerationImageError):
    code = ERROR_IMAGE_INVALID


@dataclass(frozen=True)
class ImageSource:
    """내려받을 이미지 하나. `path`는 게시물 입력의 필드 경로(예: `thumbnailUrl`)."""

    path: str
    url: str


@dataclass(frozen=True)
class ModerationImage:
    """검사가 끝난 이미지. 모델 메시지에 넣을 조각으로 바꿀 수 있다."""

    path: str
    content_type: str
    data: bytes

    def content_part(self) -> ContentPart:
        return image_part(data=self.data, content_type=self.content_type)


def detect_content_type(data: bytes) -> str | None:
    """실제 바이트로 형식을 판별한다. 지원하지 않으면 None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_url(source: ImageSource) -> httpx.URL:
    """허용된 CDN의 https 주소만 통과시킨다. 네트워크 호출은 없다.

    허용 목록 밖 주소는 `IMAGE_DOWNLOAD_FAILED`다 — 파일이 나쁜 게 아니라 접근할 수 없는
    것이라, 사용자에게 이미지 교체를 안내할 문제가 아니다.
    """
    try:
        url = httpx.URL(source.url)
    except httpx.InvalidURL as exc:
        raise ImageDownloadFailed(source.path, "이미지 URL 형식 오류") from exc
    if (
        url.scheme != "https"
        or url.host not in settings.image_parent_allowed_hosts
        or url.port not in (None, 443)
        or url.userinfo
    ):
        raise ImageDownloadFailed(source.path, "허용되지 않은 이미지 주소")
    return url


async def _download_one(client: httpx.AsyncClient, source: ImageSource) -> ModerationImage:
    """한 장을 내려받아 형식·크기를 확인한다. URL·본문은 로그에 남기지 않는다."""
    url = _validate_url(source)
    max_bytes = settings.moderation_image_max_bytes
    try:
        async with client.stream("GET", url) as response:
            if not 200 <= response.status_code < 300:
                raise ImageDownloadFailed(source.path, f"HTTP {response.status_code}")
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                # 본문을 받기 전에 끊는다 — 32MiB 넘는 파일을 다 받고 버리지 않는다.
                raise ImageInvalid(source.path, "이미지 크기 제한 초과")
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ImageInvalid(source.path, "이미지 크기 제한 초과")
    except httpx.TimeoutException as exc:
        raise ImageDownloadFailed(source.path, "이미지 다운로드 시간 초과") from exc
    except httpx.HTTPError as exc:
        raise ImageDownloadFailed(source.path, "이미지 다운로드 실패") from exc
    image_data = bytes(data)
    content_type = detect_content_type(image_data)
    if content_type is None:
        raise ImageInvalid(source.path, "이미지가 아니거나 지원하지 않는 형식")
    return ModerationImage(path=source.path, content_type=content_type, data=image_data)


async def fetch_images(sources: list[ImageSource], *, timeout: float | None = None) -> list[ModerationImage]:
    """게시물의 이미지를 전부 내려받는다. 입력 순서를 유지한다.

    한 장이라도 실패하면 `ModerationImageError`를 던진다 — 계약이 "이미지를 못 보면 승인하지
    않는다"이므로 일부만 검수하지 않는다. 파일 불량을 다운로드 실패보다 먼저 알리고,
    같은 종류의 실패는 입력 순서상 첫 번째를 알린다(`error_path`에는 하나만 싣는다).

    전체 제한 시간(`moderation_image_timeout`)은 게시물 한 건 기준이다. 장수에 비례해 늘리면
    이미지가 많은 게시물이 요청 전체 예산(150초)을 혼자 먹는다. 시간 초과 시 끝나지 않은 첫
    장을 `IMAGE_DOWNLOAD_FAILED`로 돌려준다.
    """
    if not sources:
        return []
    timeout = settings.moderation_image_timeout if timeout is None else min(timeout, settings.moderation_image_timeout)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
    encoded_bytes = 0

    async def download(source: ImageSource) -> ModerationImage:
        nonlocal encoded_bytes
        async with semaphore:
            image = await _download_one(client, source)
            # base64는 원본 3바이트마다 4바이트가 된다. 이미지들만으로 요청 한도를
            # 넘으면 모델을 부르기 전에 IMAGE_INVALID로 끝낸다. 글·JSON을 포함한
            # 최종 본문은 검수 호출을 조립하는 쪽에서 limits.validate_request_size로 검사한다.
            encoded_bytes += 4 * ((len(image.data) + 2) // 3)
            if encoded_bytes > MAX_REQUEST_BYTES:
                raise ImageInvalid(source.path, "이미지 전체 전송 용량 제한 초과")
            return image

    # 리다이렉트를 따라가지 않는다 — 허용 목록 검사를 통과한 주소가 내부 주소로 튀는 길을 막는다.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        tasks = [asyncio.create_task(download(source)) for source in sources]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            for task in tasks:
                if task.done() and not task.cancelled():
                    error = task.exception()
                    if isinstance(error, ImageInvalid):
                        raise error
            # wait_for가 gather를 취소하면 자식도 취소된다. 끝나지 않은 첫 장이 실패 위치다.
            unfinished = next(
                (s for s, t in zip(sources, tasks) if t.cancelled() or not t.done()), sources[0]
            )
            raise ImageDownloadFailed(unfinished.path, "이미지 다운로드 시간 초과") from None
    images: list[ModerationImage] = []
    for result in results:
        if isinstance(result, ImageInvalid):
            raise result
    for result in results:
        if isinstance(result, BaseException):
            # 우리 코드의 결함은 다운로드 실패로 위장하지 않는다(STYLEGUIDE §4).
            raise result
        images.append(result)
    logger.info("검수 이미지 %d장 준비 완료", len(images))
    return images
