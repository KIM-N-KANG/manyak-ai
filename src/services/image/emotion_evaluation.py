"""채팅 재료와 파일 프롬프트로 JEV를 한 번 호출한다(KNK-1451)."""

import json
from pathlib import Path
import re

from src.core.config import settings
from src.services import llm
from src.services.image.child_input import ChildImageInput
from src.services.image.emotion_selection import SelectedEmotion, select_emotions
from src.services.llm.base import ChoiceQuestion, EvaluationRequest
from src.services.prompt_meta import read_version

_PATH = Path(__file__).resolve().parents[3] / "prompt/image/JEV-EMOTION-TEMPLATE.md"
JEV_EMOTION_VERSION = read_version(_PATH)


def _load_questions() -> dict[str, ChoiceQuestion]:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", _PATH.read_text(encoding="utf-8"), re.S)
    if len(blocks) != 1:
        raise RuntimeError("JEV 프롬프트에는 questions JSON 블록 한 개가 필요합니다.")
    data = json.loads(blocks[0])
    if set(data) != {"emotion", "intensity"}:
        raise RuntimeError("JEV 프롬프트에는 emotion·intensity 질문이 필요합니다.")
    for question in data.values():
        if (question.get("type") != "choice" or not isinstance(question.get("instructions"), str)
                or not isinstance(question.get("criteria"), dict)
                or any(not isinstance(value, str) for value in question["criteria"].values())):
            raise RuntimeError("JEV 프롬프트의 질문은 문자열 지시·후보를 가진 Choice여야 합니다.")
    emotion, intensity = data["emotion"], data["intensity"]
    if set(intensity["criteria"]) != {"none", "low", "medium", "high"}:
        raise RuntimeError("JEV 강도 후보가 잘못됐습니다.")
    if any(intensity["instructions"].count(slot) != 1 for slot in ("{{emotion}}", "{{description}}")):
        raise RuntimeError("JEV 강도 지시에 감정·설명 변수가 필요합니다.")
    questions = {"emotion": ChoiceQuestion(emotion["instructions"], dict(emotion["criteria"]))}
    for name, description in emotion["criteria"].items():
        if name == "neutral":
            continue
        instructions = intensity["instructions"].replace("{{emotion}}", name).replace("{{description}}", description)
        questions[f"{name}_intensity"] = ChoiceQuestion(instructions, dict(intensity["criteria"]))
    return questions


_QUESTIONS = _load_questions()


async def evaluate_emotions(inputs: ChildImageInput, *, timeout: float) -> tuple[SelectedEmotion, ...]:
    """이미지 URL은 보내지 않는다. 호출부의 전체 시간 제한·취소가 이 호출에도 적용된다."""
    request = EvaluationRequest(
        model=settings.jev_model,
        timeout=timeout,
        state={
            "target_character": inputs.parent_image.name,
            "recent_turns": [
                {"user": turn.user_message, "assistant": turn.ai_response}
                for turn in inputs.recent_turns
            ],
            "current_turn": {"user": inputs.current_turn.user_message, "assistant": inputs.current_turn.ai_response},
        },
        questions={name: ChoiceQuestion(question.instructions, dict(question.criteria))
                   for name, question in _QUESTIONS.items()},
    )
    return select_emotions(await llm.evaluate(request))
