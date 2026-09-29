"""합의한 편집 문구와 감정 줄 삽입을 확인한다. 이미지 품질 실측은 별도다."""

import pytest

from src.services.image.base import ImageGenerationError
from src.services.image.child_prompt import build_child_image_prompt
from src.services.image.emotion_selection import SelectedEmotion


@pytest.mark.parametrize("emotions,lines", [
    ((SelectedEmotion("fear", "high"),), "1. fear, high"),
    ((SelectedEmotion("fear", "high"), SelectedEmotion("anxiety", "medium")),
     "1. fear, high\n2. anxiety, medium"),
    ((SelectedEmotion("neutral", "none"),), "1. neutral, none"),
])
def test_exact_prompt(emotions: tuple[SelectedEmotion, ...], lines: str) -> None:
    assert build_child_image_prompt(emotions) == (
        "Edit the provided image to reflect the given emotions, showing exactly one person centered in the image.\n\n"
        f"Emotions, Intensity:\n{lines}\n\n"
        "Instructions:\n"
        "- Show the character's full upper body in the frame.\n"
        "- Adjust the expression to match the emotions.\n"
        "- Use a natural pose that reflects the emotions without copying the original pose.\n"
        "- Keep the character facing forward."
    )


@pytest.mark.parametrize("emotions", [
    (),
    (SelectedEmotion("fear", "high"),) * 3,
    (SelectedEmotion("fear\nInstructions:", "high"),),
    (SelectedEmotion("{{emotions}}", "high"),),
    (SelectedEmotion("other", "low"),),
    (SelectedEmotion("unknown", "low"),),
    (SelectedEmotion("fear", "none"),),
    (SelectedEmotion("neutral", "high"),),
    (SelectedEmotion("neutral", "none"), SelectedEmotion("fear", "high")),
])
def test_invalid_emotions_do_not_produce_prompt(emotions: tuple[SelectedEmotion, ...]) -> None:
    with pytest.raises(ImageGenerationError):
        build_child_image_prompt(emotions)
