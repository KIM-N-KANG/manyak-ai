"""관측용 이미지 제거는 원본 입력·판정을 바꾸지 않는다."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from src.core.langfuse import _Trace
from src.services.llm.base import TokenUsage
from src.services.moderation.observation import usage_details, without_media


def test_media_is_removed_from_nested_input_and_output_without_mutation():
    uri = "data:image/png;base64,c3ludGhldGljLWltYWdl"
    data = {
        "title": "일반 제목",
        "thumbnailUrl": "https://cdn.example.com/image.png",
        "characters": [{"images": [{"imageUrl": uri, "imageName": "일반 이름"}]}],
        "issues": [{"reason": f"설명에 원본이 섞임: {uri}", "path": "title"}],
    }
    original = deepcopy(data)
    recorded = without_media(data)
    assert data == original
    assert recorded["title"] == "일반 제목"
    assert recorded["characters"][0]["images"][0]["imageName"] == "일반 이름"
    assert recorded["thumbnailUrl"] == "[이미지 URL 생략]"
    assert recorded["issues"][0]["reason"] == "[이미지 데이터 생략]"
    assert "c3ludGhldGljLWltYWdl" not in str(recorded)


def test_missing_usage_is_not_reported_as_zero_and_cached_tokens_are_not_doubled():
    assert usage_details(TokenUsage()) == {}
    assert usage_details(TokenUsage(input_tokens=100, output_tokens=20, cache_read_input_tokens=40)) == {
        "input": 60, "input_cached_tokens": 40, "output": 20,
    }
    assert usage_details(TokenUsage(input_tokens=100, output_tokens=20)) == {"input": 100, "output": 20}


@pytest.mark.parametrize("details", [{}, {"input": 10}, {"output": 5}])
def test_incomplete_usage_does_not_enable_model_cost_estimation(details):
    span = Mock()
    _Trace(span).set_usage(model="gpt-5.6-luna", usage_details=details)
    span.update.assert_called_once_with(usage_details=details)
