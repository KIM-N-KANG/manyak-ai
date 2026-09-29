"""선택된 감정·강도만 부모 이미지 편집 프롬프트에 삽입한다(KNK-1450)."""

from pathlib import Path
import re

from src.services.image.base import ImageGenerationError
from src.services.image.emotion_selection import SelectedEmotion
from src.services.prompt_meta import read_version

_PATH = Path(__file__).resolve().parents[3] / "prompt/image/CHILD-IMAGE-TEMPLATE.md"
CHILD_IMAGE_VERSION = read_version(_PATH)
_RAW = _PATH.read_text(encoding="utf-8")
_TEMPLATE = _RAW.split("---", 2)[2].strip()


def build_child_image_prompt(emotions: tuple[SelectedEmotion, ...]) -> str:
    """감정 1~2개를 순서대로 넣는다. 대화 원문은 받지 않는다."""
    if not 1 <= len(emotions) <= 2:
        raise ImageGenerationError("이미지 편집에는 감정 1~2개가 필요합니다.")
    for item in emotions:
        if (not re.fullmatch(r"[a-z]+", item.emotion)
                or item.emotion in {"other", "unknown"}
                or (item.emotion == "neutral" and (len(emotions) != 1 or item.intensity != "none"))
                or (item.emotion != "neutral" and item.intensity not in {"low", "medium", "high"})):
            raise ImageGenerationError("이미지 편집 감정·강도가 잘못됐습니다.")
    lines = "\n".join(
        f"{index}. {item.emotion}, {item.intensity}"
        for index, item in enumerate(emotions, start=1)
    )
    return _TEMPLATE.replace("{{emotions}}", lines)
