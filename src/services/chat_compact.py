"""대화 요약 생성과 재시도·폴백."""

import asyncio
from dataclasses import dataclass

from src.schemas.chat_compact import MAX_SUMMARY_LENGTH, ChatCompactRequest
from src.services import llm
from src.services.chat_compact_prompt import build_messages
from src.services.llm.base import LlmError, LlmRequest

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
        for _ in range(2):
            progress.attempts += 1
            progress.fallback_used = model_index > 0
            try:
                # SDK 읽기 제한과 별도로 시도 전체 경과 시간을 제한한다.
                async with asyncio.timeout(CALL_TIMEOUT):
                    result = await llm.complete(LlmRequest(
                        model=model, messages=messages, reasoning_effort="low",
                        max_tokens=MAX_TOKENS, timeout=CALL_TIMEOUT, max_retries=0,
                    ))
                if not result.text.strip():
                    raise InvalidSummary("요약 본문이 비었습니다.")
                if len(result.text) > MAX_SUMMARY_LENGTH:
                    raise InvalidSummary("요약 본문이 최대 글자 수를 초과했습니다.")
                return result.text
            except (LlmError, TimeoutError, InvalidSummary):
                continue
    raise CompactFailed("대화 요약을 생성하지 못했습니다.")
