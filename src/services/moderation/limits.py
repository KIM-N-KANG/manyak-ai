"""검수 요청의 전송 용량 제한. 공통 LLM 통로에는 검수 정책을 넣지 않는다."""

import json

# 대체 모델 DeepSeek의 inline 요청 본문 한도에 맞춘 검수 제한이다.
# 원본 이미지 합계가 아니라 base64·텍스트·JSON 필드를 포함한 크기다.
# https://api-docs.deepseek.com/guides/vision/
MAX_REQUEST_BYTES = 48 * 1024 * 1024


class ModerationRequestTooLarge(ValueError):
    """검수 요청 전체의 전송 용량이 제한을 넘었다."""


def validate_request_size(body: dict[str, object]) -> None:
    """검수 호출을 조립한 뒤, 모델 호출 전에 최종 JSON 본문의 크기를 확인한다.

    body에는 이미지·텍스트와 모델 설정을 포함한다. SDK 전용 timeout·관측 metadata는
    포함하지 않으며 extra_body의 필드는 전송 형태대로 최상위에 합친 상태로 전달한다.
    검수 API 연결 시 호출해야 하며 이 함수 자체는 모델을 부르지 않는다.
    """
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    size = 0
    for chunk in encoder.iterencode(body):
        size += len(chunk.encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise ModerationRequestTooLarge("검수 요청 전체 용량 제한(48MiB) 초과")
