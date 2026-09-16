"""자식 이미지를 백엔드가 발급한 S3 주소로 업로드한다."""

import base64
import binascii
from dataclasses import replace

import httpx

from src.core.config import settings
from src.schemas.chat_turn import ChatImageSlot
from src.services.image.generate_child import ChildImageResult


def validate_upload_url(slot: ChatImageSlot) -> httpx.URL:
    """생성 전에 저장 대상이 허용되는지 검사한다. 네트워크 호출은 하지 않는다."""
    url = httpx.URL(slot.upload_url)
    if (url.scheme != "https" or url.host not in settings.image_upload_allowed_hosts
            or url.port not in (None, 443) or url.userinfo):
        raise ValueError("허용되지 않은 업로드 주소")
    return url


async def upload_child_image(child: ChildImageResult, slot: ChatImageSlot) -> ChildImageResult:
    """재시도·리다이렉트 없이 PUT한다. 전체 마감과 취소는 호출부가 관리한다."""
    if child.error:
        return child
    try:
        url = validate_upload_url(slot)
        data = base64.b64decode(child.image_base64 or "", validate=True)
        if not data or child.content_type != "image/webp":
            raise ValueError("업로드 이미지 데이터 오류")
        request = httpx.Request(
            "PUT", url, content=data, headers={"Content-Type": child.content_type},
            extensions={"timeout": {key: settings.image_timeout for key in ("connect", "read", "write", "pool")}},
        )
        # AsyncClient.send는 서명 URL을 INFO 로그에 기록한다. 전송 계층을 직접 사용해
        # URL 로그와 자동 리다이렉트를 피하고, 응답 본문도 읽거나 기록하지 않는다.
        async with httpx.AsyncHTTPTransport(retries=0, trust_env=False) as transport:
            response = await transport.handle_async_request(request)
            try:
                if not 200 <= response.status_code < 300:
                    raise ValueError("업로드 실패")
            finally:
                await response.aclose()
        return replace(child, image_url=slot.public_url)
    except httpx.TimeoutException:
        return replace(child, image_base64=None, error="timeout")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, binascii.Error):
        # 예외 원문에는 서명 URL이 들어갈 수 있으므로 고정 코드만 반환한다.
        return replace(child, image_base64=None, error="generation_failed")
