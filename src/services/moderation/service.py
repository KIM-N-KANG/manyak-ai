"""이미지 준비 → 기본 모델 1회 → 실패 시 대체 모델 1회로 게시물을 검수한다(KNK-1360)."""

import asyncio
import logging
from dataclasses import replace

from pydantic import JsonValue

from src.core.config import settings
from src.core.sentry import ERROR_INVALID_AI_RESPONSE, FEATURE_STORY_MODERATION, capture_ai_exception
from src.services import llm
from src.services.llm.base import LlmConfigError, LlmError, LlmRequest, Message
from src.services.moderation.images import ImageDownloadFailed, ImageInvalid, fetch_images
from src.services.moderation.input import prepare_input
from src.services.moderation.limits import ModerationRequestTooLarge, validate_request_size
from src.services.moderation.models import ModelDecision, ModerationResult, failure
from src.services.moderation.prompt import build_messages
from src.services.moderation.response import InvalidModerationResponse, parse_response

logger = logging.getLogger(__name__)


def _requests(messages: list[Message]) -> tuple[LlmRequest, LlmRequest]:
    return (
        LlmRequest(
            model=settings.moderation_model, messages=messages,
            response_schema=ModelDecision.model_json_schema(), reasoning_effort="high",
            timeout=settings.moderation_call_timeout, max_retries=0,
        ),
        LlmRequest(
            model=settings.moderation_fallback_model, messages=messages, json_mode=True,
            timeout=settings.moderation_call_timeout, max_retries=0,
        ),
    )


async def moderate_story(post: dict[str, JsonValue]) -> ModerationResult:
    """저장된 게시물의 검수 결과를 반환한다. 관측용 ID는 판정에 사용하지 않는다."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.moderation_request_timeout
    inputs = prepare_input(post)
    try:
        images = await fetch_images(inputs.images, timeout=max(0.0, deadline - loop.time()))
    except ImageDownloadFailed as exc:
        capture_ai_exception(exc, feature=FEATURE_STORY_MODERATION, provider="none", error_code=exc.code)
        return failure("IMAGE_DOWNLOAD_FAILED", exc.path)
    except ImageInvalid as exc:
        capture_ai_exception(exc, feature=FEATURE_STORY_MODERATION, provider="none", error_code=exc.code)
        return failure("IMAGE_INVALID", exc.path)

    requests = _requests(build_messages(inputs, images))
    try:
        # 대체 모델에도 보낼 수 있는 용량인지 첫 모델을 부르기 전에 검사한다.
        validate_request_size(llm.request_body(requests[1]))
    except ModerationRequestTooLarge as exc:
        capture_ai_exception(
            exc, feature=FEATURE_STORY_MODERATION, provider="none",
            error_code="IMAGE_INVALID" if inputs.images else "MODEL_CALL_FAILED",
        )
        if inputs.images:
            return failure("IMAGE_INVALID", inputs.images[0].path)
        return failure("MODEL_CALL_FAILED")

    for attempt, request in enumerate(requests):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        timeout = min(settings.moderation_call_timeout, remaining)
        try:
            # SDK의 읽기 timeout과 별도로 호출 전체의 경과 시간도 제한한다.
            async with asyncio.timeout(timeout):
                result = await llm.complete(replace(request, timeout=timeout))
            logger.info(
                "검수 모델 호출 완료 model=%s input_tokens=%s output_tokens=%s",
                result.model, result.usage.input_tokens, result.usage.output_tokens,
            )
            response = parse_response(result, inputs)
            if response.error_code == "IMAGE_UNREADABLE":
                capture_ai_exception(
                    ValueError("검수 이미지 판독 실패"), feature=FEATURE_STORY_MODERATION,
                    provider=llm.provider_of(request.model), model=request.model,
                    error_code=response.error_code, retry_count=attempt,
                )
            return response
        except (LlmError, LlmConfigError, TimeoutError, InvalidModerationResponse) as exc:
            logger.warning("검수 모델 호출 실패 model=%s kind=%s", request.model, type(exc).__name__)
            capture_ai_exception(
                exc, feature=FEATURE_STORY_MODERATION, provider=llm.provider_of(request.model),
                model=request.model, retry_count=attempt,
                error_code=ERROR_INVALID_AI_RESPONSE if isinstance(exc, InvalidModerationResponse) else None,
            )
    return failure("MODEL_CALL_FAILED")
