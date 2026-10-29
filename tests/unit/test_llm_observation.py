"""공급자별 비용 항목과 누락 사용량을 검증한다."""

import pytest

from src.services.llm.base import TokenUsage
from src.services import llm


@pytest.mark.parametrize("provider", ["openai", "deepseek", "anthropic"])
def test_missing_and_zero_usage(provider):
    assert llm.usage_details(TokenUsage(), provider) == {}
    assert llm.usage_details(TokenUsage(output_tokens=20), provider) == {"output": 20}
    assert llm.usage_details(TokenUsage(0, 0), provider) == {"input": 0, "output": 0}
    assert llm.usage_details(TokenUsage(100, 20), provider) == {"input": 100, "output": 20}


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_openai_compatible_cache_is_not_counted_twice(provider):
    assert llm.usage_details(TokenUsage(100, 20, None, 40), provider) == {
        "input": 60, "input_cached_tokens": 40, "output": 20,
    }


def test_anthropic_separates_cache_creation_and_read():
    assert llm.usage_details(TokenUsage(100, 20, 10, 30), "anthropic") == {
        "input": 60, "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 30, "output": 20,
    }
