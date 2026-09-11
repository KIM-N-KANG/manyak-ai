"""KNK-1264: 이미지 호출 없이 첫 화자·부모·최근 대화 선택을 검증한다."""

import pytest

from src.schemas.chat_turn import CharacterImageMapping, ChatHistoryItem
from src.services.image.child_input import ChildImageTurn, build_child_image_input


def _image(name: str, image_name: str | None = None) -> CharacterImageMapping:
    return CharacterImageMapping(
        name=name,
        image_name=image_name if image_name is not None else f"{name}_기본",
        image_url=f"https://cdn.example.com/{name}.webp",
    )


@pytest.mark.parametrize(
    "label",
    ["라떼:", "라떼 :", "  라떼:", "**라떼:**", "**라떼**:", "\t** 라떼 ** :"],
)
def test_first_speaker_uses_basic_image_regardless_of_mapping_order(label: str) -> None:
    parent = _image("라떼")
    result = build_child_image_input(
        character_images=[
            _image("모카"), parent, _image("라떼", "라떼_웃음"),
            _image("라떼", "라떼_실시간_123"),
        ],
        history=[], user_input="문을 연다.",
        ai_output=f"*모카가 라떼를 부른다.*\n{label} 들어와.\n모카: 안녕.",
    )
    assert result is not None
    assert result.parent_image == parent
    assert result.recent_turns == ()
    assert result.current_turn.user_message == "문을 연다."
    assert "모카: 안녕." in result.current_turn.ai_response


@pytest.mark.parametrize("first_label", ["행인:", "**행인:**", "**행인**:"])
def test_speaker_without_image_is_skipped_for_next_eligible_speaker(first_label: str) -> None:
    result = build_child_image_input(
        character_images=[_image("라떼")], history=[], user_input="인사한다.",
        ai_output=f"{first_label} 안녕.\n라떼: 반가워.",
    )
    assert result is not None
    assert result.parent_image.name == "라떼"


@pytest.mark.parametrize("image_name", ["", "라떼_웃음", "라떼_실시간_123", "모카_기본", "라떼_기본_복사"])
def test_first_speaker_without_exact_basic_image_is_skipped(image_name: str) -> None:
    result = build_child_image_input(
        character_images=[_image("라떼", image_name), _image("모카")],
        history=[], user_input="인사한다.", ai_output="라떼: 안녕.\n모카: 반가워.",
    )
    assert result is not None
    assert result.parent_image.name == "모카"


@pytest.mark.parametrize("url", ["", " \t"])
def test_basic_image_without_url_is_skipped(url: str) -> None:
    parent = _image("라떼").model_copy(update={"image_url": url})
    result = build_child_image_input(
        character_images=[parent, _image("모카")], history=[],
        user_input="인사한다.", ai_output="라떼: 안녕.\n모카: 반가워.",
    )
    assert result is not None
    assert result.parent_image.name == "모카"


@pytest.mark.parametrize("output", ["", "*라떼: 안녕이라고 적힌 쪽지.*", "라떼를 부른다."])
def test_no_speaker_is_skipped(output: str) -> None:
    assert build_child_image_input(
        character_images=[_image("라떼")], history=[], user_input="인사한다.", ai_output=output,
    ) is None


@pytest.mark.parametrize(
    ("name", "label"),
    [("지한결", "한결"), ("카시안 발데르크", "카시안"), ("라_떼", "라_떼")],
)
def test_alias_resolves_to_canonical_parent_name(name: str, label: str) -> None:
    parent = _image(name)
    result = build_child_image_input(
        character_images=[parent], history=[], user_input="인사한다.",
        ai_output=f"{label}: 안녕.",
    )
    assert result is not None
    assert result.parent_image == parent


def test_alias_conflict_is_checked_before_filtering_for_basic_images() -> None:
    assert build_child_image_input(
        character_images=[_image("지한결"), _image("김한결", "김한결_웃음")],
        history=[], user_input="인사한다.", ai_output="한결: 안녕.",
    ) is None


def test_exact_name_without_parent_wins_over_another_characters_alias() -> None:
    assert build_child_image_input(
        character_images=[_image("카시안 발데르크"), _image("카시안", "카시안_웃음")],
        history=[], user_input="인사한다.", ai_output="카시안: 안녕.",
    ) is None


def test_current_turn_is_appended_once_after_two_recent_complete_pairs() -> None:
    history = [ChatHistoryItem(role="ASSISTANT", content="오프닝")]
    for number in range(1, 5):
        history.extend([
            ChatHistoryItem(role="USER", content=f"입력 {number}"),
            ChatHistoryItem(role="ASSISTANT", content=f"답변 {number}"),
        ])
    snapshot = [item.model_dump() for item in history]
    result = build_child_image_input(
        character_images=[_image("라떼")], history=history,
        user_input="입력 5", ai_output="라떼: 답변 5",
    )
    assert result is not None
    assert result.recent_turns == (
        ChildImageTurn("입력 3", "답변 3"), ChildImageTurn("입력 4", "답변 4"),
    )
    assert result.current_turn == ChildImageTurn("입력 5", "라떼: 답변 5")
    assert [item.model_dump() for item in history] == snapshot


def test_opening_and_unpaired_messages_are_not_counted_as_turns() -> None:
    history = [
        ChatHistoryItem(role="ASSISTANT", content="오프닝"),
        ChatHistoryItem(role="USER", content="짝 없는 입력"),
        ChatHistoryItem(role="USER", content="직전 입력"),
        ChatHistoryItem(role="ASSISTANT", content="직전 답변"),
        ChatHistoryItem(role="ASSISTANT", content="짝 없는 답변"),
        ChatHistoryItem(role="USER", content="미완료 입력"),
    ]
    result = build_child_image_input(
        character_images=[_image("라떼")], history=history,
        user_input="현재 입력", ai_output="라떼: 현재 답변",
    )
    assert result is not None
    assert result.recent_turns == (ChildImageTurn("직전 입력", "직전 답변"),)


def test_storage_markers_are_removed_without_changing_original_history() -> None:
    stored = "[[https://cdn.example.com/previous.webp]]\n\n라떼: 이전 답변"
    history = [
        ChatHistoryItem(role="USER", content="직전 입력"),
        ChatHistoryItem(role="ASSISTANT", content=stored),
    ]
    output = "*앞 지문*\n[[https://cdn.example.com/basic.webp]]\n\n라떼: 현재 답변"
    result = build_child_image_input(
        character_images=[_image("라떼")], history=history,
        user_input="현재 입력", ai_output=output,
    )
    assert result is not None
    assert result.recent_turns[0].ai_response == "라떼: 이전 답변"
    assert result.current_turn.ai_response == "*앞 지문*\n라떼: 현재 답변"
    assert history[1].content == stored


def test_regeneration_uses_only_the_new_response_and_has_no_previous_state() -> None:
    images = [_image("라떼"), _image("모카")]
    first = build_child_image_input(
        character_images=images, history=[], user_input="인사한다.", ai_output="라떼: 이전 답변",
    )
    regenerated = build_child_image_input(
        character_images=images, history=[], user_input="인사한다.", ai_output="모카: 새 답변",
    )
    assert first is not None and regenerated is not None
    assert first.parent_image.name == "라떼"
    assert regenerated.parent_image.name == "모카"
    assert regenerated.recent_turns == ()
    assert regenerated.current_turn == ChildImageTurn("인사한다.", "모카: 새 답변")


@pytest.mark.parametrize("output", [
    "*벽에 안내문이 붙어 있다.\n영업시간: 오후 세 시까지*\n라떼: 어서 와.",
    "행인: 안녕.\n*영업시간: 오후 세 시까지*\n라떼: 어서 와.",
])
def test_unknown_labels_in_narration_do_not_block_image_selection(output: str) -> None:
    result = build_child_image_input(
        character_images=[_image("라떼")], history=[], user_input="인사한다.", ai_output=output,
    )
    assert result is not None
    assert result.parent_image.name == "라떼"


@pytest.mark.parametrize("label", ["코드:제로:", "**코드:제로:**", "**코드:제로**:"])
def test_full_character_name_with_colon_is_not_split(label: str) -> None:
    result = build_child_image_input(
        character_images=[_image("코드"), _image("코드:제로"), _image("라떼")],
        history=[], user_input="인사한다.", ai_output=f"{label} 어서 와.\n라떼: 안녕.",
    )
    assert result is not None
    assert result.parent_image.name == "코드:제로"


@pytest.mark.parametrize("images", [[], [_image("라떼", "라떼_웃음")], [_image("모카")]])
def test_no_speaking_character_with_parent_skips_generation(images: list[CharacterImageMapping]) -> None:
    assert build_child_image_input(
        character_images=images, history=[], user_input="인사한다.", ai_output="라떼: 안녕.",
    ) is None
