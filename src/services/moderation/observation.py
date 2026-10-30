"""검수 기록에서 미디어 원본을 제거한다. 모델에 보내는 입력은 바꾸지 않는다."""

import re

from pydantic import JsonValue

_DATA_URI = re.compile(r"data:[^,\s]*;base64,", re.IGNORECASE)
_WEB_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def without_media(data: JsonValue) -> JsonValue:
    """SDK의 미디어 탐색 전에 URL·data URI를 제거한다. 출력에 되풀이된 URI도 막는다."""
    if isinstance(data, str):
        if _DATA_URI.search(data):
            return "[이미지 데이터 생략]"
        # 사유에 되풀이된 주소도 가린다. 확장자 없는 이미지 URL도 있어 형식으로 한정하지 않는다.
        return _WEB_URL.sub("[URL 생략]", data)
    if isinstance(data, list):
        return [without_media(value) for value in data]
    if isinstance(data, dict):
        return {
            key: "[이미지 URL 생략]" if key in {"imageUrl", "thumbnailUrl"} else without_media(value)
            for key, value in data.items()
        }
    return data
