"""검수 전용 용량 검사: 인코딩·본문 필드·경계값을 확인한다."""

import httpx
import pytest

from src.services.llm.base import image_part, text_part
from src.services.moderation import limits


@pytest.mark.parametrize("offset, rejected", [(0, False), (-1, True)])
def test_request_limit_counts_complete_utf8_body(monkeypatch, offset, rejected) -> None:
    body = {
        "model": "deepseek-flash",
        "messages": [{
            "role": "user",
            "content": [text_part("한글과 이미지"), image_part(data=b"abc", content_type="image/png")],
        }],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    actual_bytes = len(httpx.Request("POST", "https://example.com", json=body).content)
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", actual_bytes + offset)
    if rejected:
        with pytest.raises(limits.ModerationRequestTooLarge, match="용량 제한"):
            limits.validate_request_size(body)
    else:
        limits.validate_request_size(body)


def test_text_and_settings_can_push_request_over_limit(monkeypatch) -> None:
    body = {"messages": [{"role": "user", "content": "짧은 글"}]}
    actual_bytes = len(httpx.Request("POST", "https://example.com", json=body).content)
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", actual_bytes)
    limits.validate_request_size(body)
    body["model"] = "deepseek-flash"
    with pytest.raises(limits.ModerationRequestTooLarge):
        limits.validate_request_size(body)
