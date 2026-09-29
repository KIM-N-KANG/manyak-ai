"""JEV 판정 결과에서 자식 이미지에 전달할 감정·강도를 선택한다(KNK-1449)."""

from dataclasses import dataclass
from typing import Literal

from src.services.llm.base import EvaluationResult, LlmInvalidResponse

_SINGLE_EMOTION_THRESHOLD = 0.85
_NON_EMOTIONS = frozenset({"neutral", "other", "unknown"})


@dataclass(frozen=True)
class SelectedEmotion:
    emotion: str
    intensity: Literal["none", "low", "medium", "high"]


def select_emotions(result: EvaluationResult) -> tuple[SelectedEmotion, ...]:
    """공통 어댑터가 검증한 Choice 결과를 원래 확률 순위대로 선택한다.

    emotion과 감정별 {emotion}_intensity 질문을 받는다. 확률·confidence를
    강도로 환산하지 않는다. 동률은 JEV choice 우선, 그다음 이름순으로 정한다.
    상위 1~2개를 먼저 고르고 부적합 항목을 제외하며 3순위로 보충하지 않는다.
    빈 결과의 부모 이미지 대체는 호출부가 담당한다.
    """
    def invalid() -> LlmInvalidResponse:
        return LlmInvalidResponse(
            "JEV 감정·강도 질문 결과가 누락되거나 잘못됐습니다.",
            provider=result.provider, model=result.model,
        )

    emotion_answer = result.answers.get("emotion")
    if emotion_answer is None or not emotion_answer.probabilities:
        raise invalid()
    ranked = sorted(
        emotion_answer.probabilities.items(),
        key=lambda item: (-item[1], item[0] != emotion_answer.choice, item[0]),
    )
    first, probability = ranked[0]
    if first == "neutral":
        return (SelectedEmotion("neutral", "none"),)
    if first in {"other", "unknown"}:
        return ()

    count = 1 if probability >= _SINGLE_EMOTION_THRESHOLD else 2
    selected = []
    for emotion, probability in ranked[:count]:
        if emotion in _NON_EMOTIONS or probability <= 0:
            continue
        intensity = result.answers.get(f"{emotion}_intensity")
        if intensity is None:
            raise invalid()
        # choice는 공통 어댑터에서 최대 확률 후보임을 검증한 값이다.
        if intensity.choice in {"none", "unknown"}:
            continue
        if intensity.choice not in {"low", "medium", "high"}:
            raise invalid()
        selected.append(SelectedEmotion(emotion, intensity.choice))
    return tuple(selected)
