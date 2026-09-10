"""OpenAI Images API 어댑터(KNK-938).

gpt-image-2 계열 모델을 OpenAI SDK로 호출한다. 하는 일은 넷이다.

1. ImageRequest를 OpenAI images.generate 인자로 옮긴다.
2. 응답에서 이미지 바이너리를 꺼내 ImageResult로 만든다.
3. SDK 예외를 공급자 중립 예외로 접는다.
4. 호출 1건을 Langfuse generation 관측으로 남긴다(KNK-1240). `langfuse.openai` 자동 계측은
   images.generate를 덮지 않아 텍스트 LLM과 달리 여기서 손으로 기록한다 — 현재 트레이스
   (스토리 컴파일) 아래 자식으로 붙어 컴파일 1회의 텍스트·이미지 원가가 한 트레이스에 모인다.
"""

import base64
import binascii
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

from src.core.langfuse import observe_generation
from src.services.image.base import (
    IMAGE_PURPOSE_CHARACTER,
    IMAGE_PURPOSE_THUMBNAIL,
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

# 응답 형식. 요청 인자이자 관측 model_parameters·output 요약에 함께 적는 값이라 상수로 둔다.
_OUTPUT_FORMAT = "webp"

# 용도 → Langfuse 관측 이름. 기존 트레이스 이름("스토리 컴파일" 등)처럼 한국어로 짓는다.
_OBSERVATION_NAMES: dict[str, str] = {
    IMAGE_PURPOSE_CHARACTER: "이미지 생성:인물",
    IMAGE_PURPOSE_THUMBNAIL: "이미지 생성:썸네일",
}


def _observation_name(purpose: str) -> str:
    # 모르는 용도는 값 그대로 이름에 실어 집계에서 눈에 띄게 한다(조용히 인물로 섞이지 않게).
    return _OBSERVATION_NAMES.get(purpose, f"이미지 생성:{purpose}")


def _usage_details(response: object) -> dict[str, int] | None:
    """OpenAI Images 응답의 usage를 Langfuse usage_details로 옮긴다.

    표준 키(input·output·total)에 더해 텍스트·이미지 토큰을 나눈 세부 키를 싣는다 —
    gpt-image 계열은 텍스트 입력·이미지 입력·이미지 출력 단가가 달라 세부 키가 있어야
    비용이 맞게 계산된다(단가는 Langfuse 모델 설정에 세부 키로 등록한다). usage가 없거나
    필드가 빠진 응답은 있는 값만 싣고, 아무것도 없으면 None을 돌려준다.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details: dict[str, int] = {}
    for key, attr in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("total", "total_tokens"),
    ):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            details[key] = value
    input_details = getattr(usage, "input_tokens_details", None)
    for key, attr in (("input_text", "text_tokens"), ("input_image", "image_tokens")):
        value = getattr(input_details, attr, None)
        if isinstance(value, int):
            details[key] = value
    output_details = getattr(usage, "output_tokens_details", None)
    for key, attr in (("output_text", "text_tokens"), ("output_image", "image_tokens")):
        value = getattr(output_details, attr, None)
        if isinstance(value, int):
            details[key] = value
    return details or None


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

    output_format="webp"로 요청하면 응답의 data[0].b64_json에 base64 인코딩된
    WebP가 담긴다. gpt-image-2는 output_format으로 "b64_json"을 받지 않고
    "png"/"webp"/"jpeg"만 받되, 결과는 b64_json 필드에 base64로 준다.
    WebP는 PNG 대비 파일 크기가 작아 base64 응답 전송에 유리하다(KNK-940).
    """
    from src.core.config import settings

    client = _client(settings.openai_api_key, settings.openai_api_url)
    # 관측 블록 안에서 나가는 예외(공급자 실패·응답 해석 실패)는 관측에 ERROR로 남고 그대로
    # 전파된다. 이미지 바이너리는 관측에 싣지 않고 형식·크기만 요약한다.
    with observe_generation(
        _observation_name(req.purpose),
        model=req.model,
        model_parameters={
            "size": req.size,
            "quality": req.quality,
            "output_format": _OUTPUT_FORMAT,
        },
        input_data=req.prompt,
    ) as generation:
        try:
            response = await client.images.generate(
                model=req.model,
                prompt=req.prompt,
                n=1,
                size=req.size,
                quality=req.quality,
                output_format=_OUTPUT_FORMAT,
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

        # 토큰 사용량은 응답을 받은 즉시 기록한다 — 아래 해석이 실패해도 과금은 이미 일어났으므로
        # 원가 집계에서 빠지면 안 된다. 출력 요약은 해석이 끝난 뒤 따로 기록한다.
        generation.finish(usage_details=_usage_details(response))

        # 응답 해석 실패도 반드시 ImageGenerationError로 접는다. 인물 단위 실패 처리
        # (generate_characters._generate_one)는 이 예외만 "해당 인물 실패"로 알아듣고,
        # 다른 예외는 병렬 생성 전체를 중단시켜 성공한 인물 이미지까지 버린다(PR #92 리뷰).
        data = response.data or []
        first_image = data[0] if data else None
        b64_data = getattr(first_image, "b64_json", None)
        if not b64_data:
            raise ImageGenerationError("이미지 응답에 데이터가 없습니다.")
        if not isinstance(b64_data, str):
            raise ImageGenerationError("이미지 응답의 base64가 문자열이 아닙니다.")

        try:
            # validate=True: base64가 아닌 글자가 섞이면 조용히 건너뛰지 않고 실패시킨다.
            image_bytes = base64.b64decode(b64_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenerationError(f"이미지 응답의 base64가 잘못됐습니다: {exc}") from exc

        generation.finish(output={"format": _OUTPUT_FORMAT, "bytes": len(image_bytes)})

    return ImageResult(
        image_bytes=image_bytes,
        model=req.model,
        provider=PROVIDER_OPENAI,
    )
