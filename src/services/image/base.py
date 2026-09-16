"""이미지 생성 통로의 공용 타입 — 요청·결과·공급자 중립 예외(KNK-938).

텍스트 LLM 통로(src/services/llm/)와 같은 3층 구조의 바닥층이다.
호출부와 어댑터가 주고받는 말을 여기서 정한다.
"""

from dataclasses import dataclass

# 어댑터 종류 — 텍스트 LLM과 이름 공간이 겹치지 않게 접두어를 붙인다.
ADAPTER_OPENAI_IMAGE = "openai_image"

# 공급자 식별자 — 텍스트 LLM과 같은 값을 공유한다(로깅·Sentry 태그 일관성).
PROVIDER_OPENAI = "openai"

# 이미지 용도 — Langfuse 관측 이름을 가르는 값(KNK-1240). 인물 이미지와 썸네일의
# 장수·실패율을 따로 봐야 하므로 호출부가 요청에 실어 보낸다.
IMAGE_PURPOSE_CHARACTER = "character"
IMAGE_PURPOSE_THUMBNAIL = "thumbnail"
IMAGE_PURPOSE_CHILD = "child"


@dataclass(frozen=True)
class ImageReference:
    image_bytes: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class ImageRequest:
    """이미지 생성 요청. 호출부가 채운다."""

    model: str
    prompt: str
    purpose: str  # IMAGE_PURPOSE_* — 관측 이름 구분용
    size: str = "1024x1024"
    quality: str = "low"
    timeout: float = 60.0
    reference: ImageReference | None = None


@dataclass(frozen=True)
class ImageResult:
    """이미지 생성 결과. 어댑터가 채운다."""

    image_bytes: bytes  # 이미지 바이너리 (현재 WebP, output_format에 따라 변경 가능)
    model: str
    provider: str


# ── 공급자 중립 예외 ──────────────────────────────────────────────────────────
# 텍스트 LLM 예외(LlmError)와 별도 계보를 둔다. 호출부가 이미지 실패와
# 텍스트 실패를 구분해서 처리하기 위해서다(이미지 실패는 502가 아니라 해당
# 인물만 이미지 없이 두는 게 올바른 동작).


class ImageGenerationError(Exception):
    """이미지 생성에 실패했다 — 재시도 불가인 공급자 오류를 감싼다."""


class ImageTimeout(ImageGenerationError):
    """이미지 생성 시간 초과."""


class ImageRateLimited(ImageGenerationError):
    """이미지 생성 속도 제한."""


class ImageBadRequest(ImageGenerationError):
    """프롬프트 거부 등 요청 자체의 문제."""
