"""검수 모델의 출력과 서버가 정리한 결과. HTTP 입력 검증은 API 티켓에서 담당한다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Rule = Literal[
    "MINOR_SEXUAL_EXPLOITATION", "EXPLICIT_SEXUAL_CONTENT", "NONCONSENSUAL_SEXUAL_EXPLOITATION",
    "DRUGS", "EXTREME_GORE", "SELF_HARM_PROMOTION", "HATE_VIOLENCE_INCITEMENT",
]
ImageErrorCode = Literal["IMAGE_DOWNLOAD_FAILED", "IMAGE_INVALID", "IMAGE_UNREADABLE"]
ErrorCode = Literal[ImageErrorCode, "MODEL_CALL_FAILED"]


class ModelIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    rule: Rule | None
    reason: str


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["APPROVED", "REJECTED"]
    issues: list[ModelIssue]


class ModerationIssue(BaseModel):
    path: str
    type: Literal["TEXT", "IMAGE"]
    rule: Rule
    reason: str


class ModerationImageFailure(BaseModel):
    path: str
    error_code: ImageErrorCode


class ModerationResult(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    issues: list[ModerationIssue] = Field(default_factory=list)
    error_code: ErrorCode | None = None
    image_errors: list[ModerationImageFailure] = Field(default_factory=list)


def failure(
    code: ErrorCode, image_errors: list[ModerationImageFailure] | None = None,
    *, issues: list[ModerationIssue] | None = None,
) -> ModerationResult:
    return ModerationResult(decision="REJECTED", issues=issues or [], error_code=code, image_errors=image_errors or [])
