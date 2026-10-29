"""공통 LLM 결과를 관측용 사용량으로 변환한다."""

from src.services.llm.base import PROVIDER_ANTHROPIC, TokenUsage


def usage_details(usage: TokenUsage, provider: str) -> dict[str, int]:
    """캐시를 입력 합계에서 분리하고 공급자별 Langfuse 비용 항목으로 변환한다."""
    details: dict[str, int] = {}
    if usage.input_tokens is not None:
        details["input"] = usage.input_tokens - (usage.cache_read_input_tokens or 0) - (
            usage.cache_creation_input_tokens or 0
        )
        if usage.cache_read_input_tokens is not None:
            key = "cache_read_input_tokens" if provider == PROVIDER_ANTHROPIC else "input_cached_tokens"
            details[key] = usage.cache_read_input_tokens
        if usage.cache_creation_input_tokens is not None:
            details["cache_creation_input_tokens"] = usage.cache_creation_input_tokens
    if usage.output_tokens is not None:
        details["output"] = usage.output_tokens
    return details
