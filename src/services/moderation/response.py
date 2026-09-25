"""모델이 돌려준 경로와 판정을 검증하고 API 결과 형식으로 정리한다."""

from pydantic import ValidationError

from src.services.llm.base import LlmResult
from src.services.moderation.input import ModerationInput
from src.services.moderation.models import ModelDecision, ModerationIssue, ModerationResult, failure


class InvalidModerationResponse(ValueError):
    """모델 응답을 신뢰할 수 없어 대체 호출이 필요하다. 원문은 예외에 담지 않는다."""


def parse_response(result: LlmResult, inputs: ModerationInput) -> ModerationResult:
    if result.finish_reason not in (None, "stop"):
        raise InvalidModerationResponse("검수 응답이 정상 종료되지 않았습니다")
    try:
        parsed = ModelDecision.model_validate_json(result.text)
    except ValidationError:
        raise InvalidModerationResponse("검수 응답 형식 오류") from None
    if parsed.decision == "APPROVED":
        if parsed.issues:
            raise InvalidModerationResponse("승인 판정에 문제 항목이 있습니다")
        return ModerationResult(decision="APPROVED")

    valid = [issue for issue in parsed.issues if issue.path in inputs.paths]
    if not valid or any(not issue.reason.strip() for issue in valid):
        raise InvalidModerationResponse("검수 거절의 유효한 근거가 없습니다")
    unreadable: set[str] = set()
    issues: list[ModerationIssue] = []
    for issue in valid:
        kind = inputs.paths[issue.path]
        if issue.rule is None:
            if kind != "IMAGE":
                raise InvalidModerationResponse("텍스트 경로에 이미지 판독 실패가 있습니다")
            unreadable.add(issue.path)
        else:
            issues.append(ModerationIssue(path=issue.path, type=kind, rule=issue.rule, reason=issue.reason))
    if unreadable:
        path = next(source.path for source in inputs.images if source.path in unreadable)
        return failure("IMAGE_UNREADABLE", path)
    return ModerationResult(decision="REJECTED", issues=issues)
