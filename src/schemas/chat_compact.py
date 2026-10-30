"""대화 요약 요청과 응답 계약."""

from typing import Self

from pydantic import BaseModel, Field, model_validator

from src.schemas.chat_turn import ChatHistoryItem
from src.services.chat_image_markers import strip_character_image_syntax

MAX_SUMMARY_LENGTH = 3_000


class ChatCompactRequest(BaseModel):
    """기존 요약과 현재 완료 턴까지 포함한 미요약 대화."""

    summary: str
    history: list[ChatHistoryItem]

    @model_validator(mode="after")
    def require_summary_content(self) -> Self:
        """이미지 마커와 공백만 있는 요청은 모델 호출 전에 거부한다."""
        if not strip_character_image_syntax(self.summary).strip() and not any(
            strip_character_image_syntax(item.content).strip() for item in self.history
        ):
            raise ValueError("요약할 대화 내용이 없습니다.")
        return self


class ChatCompactResponse(BaseModel):
    """갱신된 대화 요약."""

    summary: str = Field(max_length=MAX_SUMMARY_LENGTH)
