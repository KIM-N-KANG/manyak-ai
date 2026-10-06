"""제작자가 등록한 이미지 이름 중 대사 친 인물마다 대화 상황에 맞는 한 장을 선택한다(KNK-1505)."""

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from src.core.config import settings
from src.schemas.chat_turn import CharacterImageMapping, ChatHistoryItem
from src.services import llm
from src.services.chat_llm import speaking_character_names
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.llm.base import (
    ChoiceQuestion,
    EvaluationRequest,
    LlmConfigError,
    LlmError,
    TokenUsage,
)
from src.services.prompt_meta import read_version

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parents[2] / "prompt/chat/IMAGE-SELECTION-TEMPLATE.md"
IMAGE_SELECTION_VERSION = read_version(_PATH)


def _load_instructions() -> str:
    text = _PATH.read_text(encoding="utf-8")
    if text.count("## [INSTRUCTIONS]") != 1:
        raise RuntimeError("이미지 선택 프롬프트에 INSTRUCTIONS 블록 한 개가 필요합니다.")
    instructions = text.split("## [INSTRUCTIONS]", 1)[1].strip()
    if not instructions:
        raise RuntimeError("이미지 선택 지시가 비어 있습니다.")
    return instructions


_INSTRUCTIONS = _load_instructions()


@dataclass(frozen=True)
class ImageSelectionResult:
    """선택된 인물별 이미지. 실패 시 단일 후보만 유지하고 기본 이미지 대체는 연결부가 맡는다."""

    images: list[CharacterImageMapping] = field(default_factory=list, repr=False)
    usage: TokenUsage = field(default_factory=TokenUsage)


def _recent_turns(history: list[ChatHistoryItem]) -> list[dict[str, str]]:
    """완성된 USER·ASSISTANT 쌍 두 개를 시간순으로 반환한다. 원본은 보존한다."""
    turns = []
    for index in range(len(history) - 2, -1, -1):
        user, assistant = history[index], history[index + 1]
        if user.role == "USER" and assistant.role == "ASSISTANT":
            turns.append({
                "user": strip_character_image_syntax(user.content),
                "assistant": strip_character_image_syntax(assistant.content),
            })
            if len(turns) == 2:
                break
    return list(reversed(turns))


async def select_images(
    *,
    character_images: list[CharacterImageMapping],
    history: list[ChatHistoryItem],
    user_input: str,
    ai_output: str,
    timeout: float = 10.0,
) -> ImageSelectionResult:
    """이름 목록·이전 2턴·이번 대화로 단발 선택한다. URL·그림은 Jev에 보내지 않는다.

    후보가 한 장이면 호출 없이 반환한다. 호출 실패·키 누락은 선택 없음으로 처리하고,
    외부 취소는 전파한다. timeout은 이번 선택 호출의 최대 대기 시간이다.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        return ImageSelectionResult()
    candidates = [
        image for image in character_images
        if image.name.strip() and image.image_name.strip() and image.image_url.strip()
    ]
    if not candidates:
        return ImageSelectionResult()
    speakers = speaking_character_names(ai_output, character_images)
    groups = {name: [image for image in candidates if image.name == name] for name in speakers}
    selected = {name: images[0] for name, images in groups.items() if len(images) == 1}
    by_question = {}
    questions = {}
    for index, (name, images) in enumerate(groups.items(), start=1):
        if not 2 <= len(images) <= 255:
            continue
        question_id = str(index)
        by_id = {str(i): image for i, image in enumerate(images, start=1)}
        by_question[question_id] = by_id
        questions[question_id] = ChoiceQuestion(
            instructions=_INSTRUCTIONS.replace("{{character_name}}", json.dumps(name, ensure_ascii=False)),
            criteria={key: image.image_name for key, image in by_id.items()},
        )
    if not questions:
        return ImageSelectionResult(images=list(selected.values()))
    request = EvaluationRequest(
        model=settings.jev_model,
        state={
            "recent_turns": _recent_turns(history),
            "current_turn": {
                "user": strip_character_image_syntax(user_input),
                "assistant": strip_character_image_syntax(ai_output),
            },
        },
        questions=questions,
        timeout=timeout,
    )
    try:
        async with asyncio.timeout(timeout):
            result = await llm.evaluate(request)
    except (LlmError, LlmConfigError, TimeoutError) as exc:
        # 공급자 응답·URL·대화 원문이 로그에 섞이지 않도록 예외 종류만 기록한다.
        logger.warning("이미지 선택 실패: %s", type(exc).__name__)
        return ImageSelectionResult(images=list(selected.values()))
    for question_id, by_id in by_question.items():
        answer = result.answers.get(question_id)
        image = by_id.get(answer.choice) if answer is not None else None
        if image is not None:
            selected[image.name] = image
    return ImageSelectionResult(images=[selected[name] for name in speakers if name in selected], usage=result.usage)
