"""자연어 부모 편집 지시에 인물 이름과 대화를 인용문으로 삽입한다."""

from pathlib import Path

from src.services.image.child_input import ChildImageInput
from src.services.prompt_meta import read_version

_PATH = Path(__file__).resolve().parents[3] / "prompt/image/CHILD-IMAGE-TEMPLATE.md"
CHILD_IMAGE_VERSION = read_version(_PATH)
_RAW = _PATH.read_text(encoding="utf-8")
_TEMPLATE = _RAW[_RAW.index("### 작업 지시"):].strip()


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.split("\n"))


def build_child_image_prompt(inputs: ChildImageInput) -> str:
    """대화의 태그·자리표시자를 명령으로 재해석하지 않고 한 번만 치환한다."""
    sections = ["## dialogue_input", f"target_character:\n{_quote(inputs.parent_image.name)}", "### recent_turns"]
    for index, turn in enumerate(inputs.recent_turns, start=-len(inputs.recent_turns)):
        sections.append(
            f"#### turn (relative_to_current: {index})\n\n"
            f"user_message:\n{_quote(turn.user_message)}\n\n"
            f"ai_response:\n{_quote(turn.ai_response)}"
        )
    sections.append(
        f"### current_turn\n\n"
        f"user_message:\n{_quote(inputs.current_turn.user_message)}\n\n"
        f"ai_response:\n{_quote(inputs.current_turn.ai_response)}"
    )
    return _TEMPLATE.replace("{{dialogue_input}}", "\n\n".join(sections))
