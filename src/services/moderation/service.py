"""이미지 준비 → 기본 모델 1회 → 실패 시 대체 모델 1회로 게시물을 검수한다(KNK-1360)."""

import asyncio
import logging
from dataclasses import replace

from pydantic import JsonValue

from src.core.config import settings
from src.core.langfuse import observe_model_call
from src.core.sentry import ERROR_INVALID_AI_RESPONSE, FEATURE_STORY_MODERATION, capture_ai_exception
from src.services import llm
from src.services.llm.base import LlmConfigError, LlmError, LlmRequest, Message
from src.services.moderation.images import fetch_images
from src.services.moderation.input import prepare_input
from src.services.moderation.limits import ModerationRequestTooLarge, validate_request_size
from src.services.moderation.models import ImageErrorCode, ModelDecision, ModerationCall, ModerationImageFailure, ModerationResult, failure
from src.services.moderation.prompt import build_messages
from src.services.moderation.observation import usage_details, without_media
from src.services.moderation.response import InvalidModerationResponse, parse_response

logger = logging.getLogger(__name__)


def _requests(messages: list[Message]) -> tuple[LlmRequest, LlmRequest]:
    return (
        LlmRequest(
            model=settings.moderation_model, messages=messages,
            response_schema=ModelDecision.model_json_schema(), reasoning_effort="high",
            timeout=settings.moderation_call_timeout, max_retries=0,
            automatic_observation=False,
        ),
        LlmRequest(
            model=settings.moderation_fallback_model, messages=messages, json_mode=True,
            timeout=settings.moderation_call_timeout, max_retries=0,
            automatic_observation=False,
        ),
    )


def _representative_code(errors: list[ModerationImageFailure]) -> ImageErrorCode:
    """교체할 이미지를 먼저 알린다. 두 교체 사유가 섞이면 파일 불량을 대표 코드로 쓴다."""
    return next(code for code in ("IMAGE_INVALID", "IMAGE_UNREADABLE", "IMAGE_DOWNLOAD_FAILED")
                if any(error.error_code == code for error in errors))


async def moderate_story(
    post: dict[str, JsonValue], *, calls: list[ModerationCall] | None = None,
) -> ModerationResult:
    """저장된 게시물의 검수 결과를 반환한다. 관측용 ID는 판정에 사용하지 않는다."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.moderation_request_timeout
    inputs = prepare_input(post)
    prepared = await fetch_images(inputs.images, timeout=max(0.0, deadline - loop.time()))
    image_errors = [ModerationImageFailure(path=exc.path, error_code=exc.code) for exc in prepared.errors]
    if prepared.errors:
        # 요청당 한 건으로 보고한다. 이미지마다 보내면 CDN 장애 한 번에 최대 60건이 쌓인다.
        capture_ai_exception(
            ExceptionGroup(f"검수 이미지 준비 실패 {len(prepared.errors)}장", prepared.errors),
            feature=FEATURE_STORY_MODERATION, provider="none", error_code=_representative_code(image_errors),
        )

    def merge_errors(response: ModerationResult) -> ModerationResult:
        errors = {error.path: error for error in [*image_errors, *response.image_errors]}
        if not errors:
            return response
        ordered = [errors[source.path] for source in inputs.images if source.path in errors]
        return failure(_representative_code(ordered), ordered, issues=response.issues)

    if image_errors and not prepared.images:
        return merge_errors(ModerationResult(decision="REJECTED"))
    # 실패한 URL은 모델 입력·판정 가능 경로에서 제외하되 배열 인덱스는 바꾸지 않는다.
    model_inputs = prepare_input(post, excluded_image_paths={error.path for error in image_errors})
    requests = _requests(build_messages(model_inputs, prepared.images))
    try:
        # 대체 모델에도 보낼 수 있는 용량인지 첫 모델을 부르기 전에 검사한다.
        validate_request_size(llm.request_body(requests[1]))
    except ModerationRequestTooLarge as exc:
        capture_ai_exception(
            exc, feature=FEATURE_STORY_MODERATION, provider="none",
            error_code="IMAGE_INVALID" if prepared.images else "MODEL_CALL_FAILED",
        )
        if prepared.images:
            return merge_errors(failure("IMAGE_INVALID", [
                ModerationImageFailure(path=image.path, error_code="IMAGE_INVALID")
                for image in prepared.images
            ]))
        return merge_errors(failure("MODEL_CALL_FAILED"))

    for attempt, request in enumerate(requests):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        timeout = min(settings.moderation_call_timeout, remaining)
        call = ModerationCall(attempt=attempt + 1, model=request.model, provider=llm.provider_of(request.model))
        started = loop.time()
        try:
            with observe_model_call(
                "검수 모델 호출", model=request.model, provider=call.provider, attempt=attempt + 1,
                input_data={
                    "post": without_media(model_inputs.post),
                    "images": [{"path": image.path, "content_type": image.content_type,
                                "size_bytes": len(image.data)} for image in prepared.images],
                },
                metadata=llm.observation_metadata(request.model),
            ) as observation:
                # SDK의 읽기 timeout과 별도로 호출 전체의 경과 시간도 제한한다.
                async with asyncio.timeout(timeout):
                    result = await llm.complete(replace(request, timeout=timeout))
                call.model = result.model
                call.provider = result.provider
                call.input_tokens = result.usage.input_tokens
                call.output_tokens = result.usage.output_tokens
                observation.set_usage(model=result.model, usage_details=usage_details(result.usage))
                logger.info(
                    "검수 모델 호출 완료 model=%s input_tokens=%s output_tokens=%s",
                    result.model, result.usage.input_tokens, result.usage.output_tokens,
                )
                observation.set_metadata(
                    model=result.model, provider=result.provider,
                    input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
                )
                response = parse_response(result, model_inputs)
                observation.set_output(without_media(response.model_dump(mode="json")))
                call.result = response
                if response.error_code == "IMAGE_UNREADABLE":
                    capture_ai_exception(
                        ValueError("검수 이미지 판독 실패"), feature=FEATURE_STORY_MODERATION,
                        provider=llm.provider_of(request.model), model=request.model,
                        error_code=response.error_code, retry_count=attempt,
                    )
                return merge_errors(response)
        except (LlmError, LlmConfigError, TimeoutError, InvalidModerationResponse) as exc:
            call.error_type = type(exc).__name__
            logger.warning("검수 모델 호출 실패 model=%s kind=%s", request.model, type(exc).__name__)
            capture_ai_exception(
                exc, feature=FEATURE_STORY_MODERATION, provider=llm.provider_of(request.model),
                model=request.model, retry_count=attempt,
                error_code=ERROR_INVALID_AI_RESPONSE if isinstance(exc, InvalidModerationResponse) else None,
            )
        except BaseException as exc:
            # 취소·예상 밖 오류도 호출 기록만 남기고 원래 예외를 그대로 전파한다.
            call.error_type = type(exc).__name__
            raise
        finally:
            call.duration_ms = (loop.time() - started) * 1000
            if calls is not None:
                calls.append(call)
    return merge_errors(failure("MODEL_CALL_FAILED"))
