"""감정 선택 정책은 실제 공급자 호출 없이 확률 경계와 독립 강도로 검증한다."""

from copy import deepcopy

import pytest

from src.services.image.emotion_selection import SelectedEmotion, select_emotions
from src.services.llm.base import ChoiceAnswer, EvaluationResult, LlmInvalidResponse, TokenUsage


def result_for(
    probabilities: dict[str, float],
    intensities: dict[str, str] | None = None,
    *,
    choice: str | None = None,
) -> EvaluationResult:
    answers = {
        "emotion": ChoiceAnswer(
            choice=choice or max(probabilities, key=probabilities.get),
            probabilities=probabilities,
            confidence=0.1,
        ),
    }
    for emotion in probabilities:
        if emotion in {"neutral", "other", "unknown"}:
            continue
        intensity = (intensities or {}).get(emotion, "high")
        answers[f"{emotion}_intensity"] = ChoiceAnswer(
            choice=intensity,
            probabilities={level: float(level == intensity)
                           for level in ("none", "low", "medium", "high", "unknown")},
            confidence=0.1,
        )
    return EvaluationResult(answers, "jev-1.13.0", "typesafe", TokenUsage())


@pytest.mark.parametrize("probability,count", [
    (0.849999, 2), (0.85, 1), (0.850001, 1), (1.0, 1),
])
def test_single_emotion_threshold(probability: float, count: int) -> None:
    result = result_for({"fear": probability, "anxiety": 1 - probability})
    assert select_emotions(result) == (
        SelectedEmotion("fear", "high"), SelectedEmotion("anxiety", "high"),
    )[:count]


@pytest.mark.parametrize("probabilities,intensities,expected", [
    ({"surprise": .69, "excitement": .17, "neutral": .14}, {},
     (SelectedEmotion("surprise", "high"), SelectedEmotion("excitement", "high"))),
    ({"fear": .81, "anxiety": .18, "unknown": .01}, {},
     (SelectedEmotion("fear", "high"), SelectedEmotion("anxiety", "high"))),
    ({"jealousy": .35, "worry": .20, "joy": .16, "hurt": .15, "neutral": .14},
     {"jealousy": "medium", "worry": "medium"},
     (SelectedEmotion("jealousy", "medium"), SelectedEmotion("worry", "medium"))),
    ({"fear": .84, "anxiety": .09, "joy": .07}, {"fear": "low", "anxiety": "high"},
     (SelectedEmotion("fear", "low"), SelectedEmotion("anxiety", "high"))),
])
def test_original_probabilities_and_each_emotions_own_intensity(
    probabilities: dict[str, float], intensities: dict[str, str],
    expected: tuple[SelectedEmotion, ...],
) -> None:
    result = result_for(probabilities, intensities)
    original = deepcopy(result)
    assert select_emotions(result) == expected
    assert result == original


@pytest.mark.parametrize("special", ["other", "unknown"])
def test_unusable_first_emotion_does_not_promote_second(special: str) -> None:
    assert select_emotions(result_for({special: .6, "fear": .4})) == ()


def test_neutral_is_always_alone_without_intensity_question() -> None:
    assert select_emotions(result_for({"neutral": .6, "fear": .4})) == (
        SelectedEmotion("neutral", "none"),
    )


@pytest.mark.parametrize("special", ["neutral", "other", "unknown"])
def test_unusable_second_emotion_is_not_replaced_by_third(special: str) -> None:
    assert select_emotions(result_for({"fear": .5, special: .3, "anxiety": .2})) == (
        SelectedEmotion("fear", "high"),
    )


@pytest.mark.parametrize("intensity", ["none", "unknown"])
def test_unusable_intensity_is_removed_without_refilling(intensity: str) -> None:
    result = result_for({"fear": .5, "anxiety": .3, "joy": .2}, {"fear": intensity})
    assert select_emotions(result) == (SelectedEmotion("anxiety", "high"),)
    assert select_emotions(result_for({"fear": .9, "joy": .1}, {"fear": intensity})) == ()


def test_ties_use_choice_then_name_independently_of_json_key_order() -> None:
    for names in (("joy", "fear", "anger"), ("anger", "fear", "joy")):
        result = result_for(dict.fromkeys(names, 1 / 3), choice="joy")
        assert select_emotions(result) == (
            SelectedEmotion("joy", "high"), SelectedEmotion("anger", "high"),
        )


@pytest.mark.parametrize("missing", ["emotion", "fear_intensity"])
def test_missing_required_answer_raises_common_error(missing: str) -> None:
    result = result_for({"fear": .9, "joy": .1})
    del result.answers[missing]
    with pytest.raises(LlmInvalidResponse) as caught:
        select_emotions(result)
    assert caught.value.model == result.model
    assert caught.value.provider == result.provider


def test_unknown_intensity_label_is_not_sent_to_image() -> None:
    result = result_for({"fear": .9, "joy": .1}, {"fear": "invalid"})
    with pytest.raises(LlmInvalidResponse):
        select_emotions(result)
