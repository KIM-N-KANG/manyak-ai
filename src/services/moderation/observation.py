"""검수 기록에서 미디어 원본을 제거한다. 모델에 보내는 입력은 바꾸지 않는다."""

import re

from pydantic import JsonValue

from src.services.llm.base import TokenUsage

_DATA_URI = re.compile(r"data:[^,\s]*;base64,", re.IGNORECASE)


def without_media(data: JsonValue) -> JsonValue:
    """SDK의 미디어 탐색 전에 URL·data URI를 제거한다. 출력에 되풀이된 URI도 막는다."""
    if isinstance(data, str):
        return "[이미지 데이터 생략]" if _DATA_URI.search(data) else data
    if isinstance(data, list):
        return [without_media(value) for value in data]
    if isinstance(data, dict):
        return {
            key: "[이미지 URL 생략]" if key in {"imageUrl", "thumbnailUrl"} else without_media(value)
            for key, value in data.items()
        }
    return data


def usage_details(usage: TokenUsage) -> dict[str, int]:
    """입력 합계에 이미 포함된 캐시 토큰을 분리해 중복 과금을 막는다."""
    details = {}
    if usage.input_tokens is not None:
        cached = usage.cache_read_input_tokens or 0
        details["input"] = usage.input_tokens - cached
        if usage.cache_read_input_tokens is not None:
            details["input_cached_tokens"] = cached
    if usage.output_tokens is not None:
        details["output"] = usage.output_tokens
    return details
