"""자식 이미지 생성 재료: 부모가 있는 첫 화자의 기본 이미지와 현재 턴을 포함한 최근 3턴.

이미지 호출·채팅 스트림 연결은 후속 작업에서 이 조립 결과를 사용한다(KNK-1264).
"""

from dataclasses import dataclass

from src.schemas.chat_turn import CharacterImageMapping, ChatHistoryItem
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.chat_llm import find_first_parent_image


@dataclass(frozen=True)
class ChildImageTurn:
    user_message: str
    ai_response: str


@dataclass(frozen=True)
class ChildImageInput:
    parent_image: CharacterImageMapping
    recent_turns: tuple[ChildImageTurn, ...]
    current_turn: ChildImageTurn


def build_child_image_input(
    *,
    character_images: list[CharacterImageMapping],
    history: list[ChatHistoryItem],
    user_input: str,
    ai_output: str,
) -> ChildImageInput | None:
    """부모 이미지가 있는 인물 중 첫 화자를 선택한다. history는 이번 턴을 제외한 요청 이력이다.

    재생성도 백엔드가 이전 답변을 뺀 history와 같은 user_input을 보내므로 동일하게 조립한다.
    짝 없는 ASSISTANT(오프닝 포함)·USER는 턴으로 세지 않는다. 요청 원본은 수정하지 않는다.
    """
    clean_output = strip_character_image_syntax(ai_output)
    parent = find_first_parent_image(clean_output, character_images)
    if parent is None:
        return None

    recent_turns: list[ChildImageTurn] = []
    for index in range(len(history) - 2, -1, -1):
        user, assistant = history[index], history[index + 1]
        if user.role == "USER" and assistant.role == "ASSISTANT":
            recent_turns.append(
                ChildImageTurn(
                    user_message=strip_character_image_syntax(user.content),
                    ai_response=strip_character_image_syntax(assistant.content),
                )
            )
            if len(recent_turns) == 2:
                break

    return ChildImageInput(
        parent_image=parent,
        recent_turns=tuple(reversed(recent_turns)),
        current_turn=ChildImageTurn(user_message=user_input, ai_response=clean_output),
    )
