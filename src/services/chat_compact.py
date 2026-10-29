"""대화 요약 생성과 시도별 재시도·폴백·관측."""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

from src.core.langfuse import observe_model_call
from src.core.sentry import capture_ai_exception, classify_error_code
from src.schemas.chat_compact import MAX_SUMMARY_LENGTH, ChatCompactRequest
from src.services import llm
from src.services.chat_compact_prompt import COMPACT_VERSION, build_messages
from src.services.llm.base import LlmError, LlmRequest

logger = logging.getLogger(__name__)

MODELS = llm.compact_models()
CALL_TIMEOUT = 60.0
MAX_TOKENS = 12_288


class CompactFailed(Exception):
    """허용된 모든 시도에서 유효한 요약을 얻지 못했다."""


class InvalidSummary(Exception):
    """요약 본문이 비었거나 허용 글자 수를 초과했다."""


@dataclass
class CompactProgress:
    attempts: int = 0
    fallback_used: bool = False


async def generate_compact(request: ChatCompactRequest, progress: CompactProgress) -> str:
    messages = build_messages(request)
    for model_index, model in enumerate(MODELS):
        provider = llm.provider_of(model)
        for _ in range(2):
            progress.attempts += 1
            progress.fallback_used = model_index > 0
            started = time.monotonic()
            with observe_model_call(
                "대화 요약 모델 호출", model=model, provider=provider,
                attempt=progress.attempts, input_data=messages,
                metadata={**llm.observation_metadata(model), "prompt_versions": {"COMPACT": COMPACT_VERSION}},
            ) as observation:
                status = "failed"
                try:
                    # SDK 읽기 제한과 별도로 시도 전체 경과 시간을 제한한다.
                    async with asyncio.timeout(CALL_TIMEOUT):
                        result = await llm.complete(LlmRequest(
                            model=model, messages=messages, reasoning_effort="low",
                            max_tokens=MAX_TOKENS, timeout=CALL_TIMEOUT, max_retries=0,
                            automatic_observation=False,
                        ))
                    observation.set_usage(model=result.model, usage_details=llm.usage_details(result.usage, result.provider))
                    observation.set_metadata(model=result.model, provider=result.provider, **asdict(result.usage))
                    observation.set_output(result.text)
                    if not result.text.strip():
                        raise InvalidSummary("요약 본문이 비었습니다.")
                    if len(result.text) > MAX_SUMMARY_LENGTH:
                        raise InvalidSummary("요약 본문이 최대 글자 수를 초과했습니다.")
                    status = "success"
                    return result.text
                except (LlmError, TimeoutError, InvalidSummary) as exc:
                    code = ("invalid_ai_response" if isinstance(exc, InvalidSummary) else
                            "provider_timeout" if isinstance(exc, TimeoutError) else classify_error_code(exc))
                    observation.mark_failed(exc)
                    observation.set_metadata(error_code=code)
                    logger.warning("compact 시도 실패 model=%s attempt=%d error_code=%s", model, progress.attempts, code)
                    # 공급자 예외의 메시지·연쇄에는 원문이 들어갈 수 있어 새 예외만 기록한다.
                    capture_ai_exception(
                        RuntimeError("compact model attempt failed"), feature="chat_compact",
                        provider=provider, model=model, error_code=code,
                        prompt_versions={"COMPACT": COMPACT_VERSION}, retry_count=progress.attempts - 1,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                finally:
                    observation.set_metadata(status=status, duration_ms=(time.monotonic() - started) * 1000)
    raise CompactFailed("대화 요약을 생성하지 못했습니다.")
