"""부모 편집 지시에 인물 이름과 대화를 XML 텍스트로 삽입한다."""

from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from src.services.image.child_input import ChildImageInput
from src.services.prompt_meta import read_version

_PATH = Path(__file__).resolve().parents[3] / "prompt/image/CHILD-IMAGE-TEMPLATE.md"
CHILD_IMAGE_VERSION = read_version(_PATH)
_RAW = _PATH.read_text(encoding="utf-8")
_TEMPLATE = _RAW[_RAW.index("<image_edit_request>"):].strip()


def build_child_image_prompt(inputs: ChildImageInput) -> str:
    """대화의 태그·자리표시자를 명령으로 재해석하지 않고 한 번만 치환한다."""
    root = Element("dialogue_input")
    SubElement(root, "target_character").text = inputs.parent_image.name
    recent = SubElement(root, "recent_turns")
    for index, turn in enumerate(inputs.recent_turns, start=-len(inputs.recent_turns)):
        node = SubElement(recent, "turn", relative_to_current=str(index))
        SubElement(node, "user_message").text = turn.user_message
        SubElement(node, "ai_response").text = turn.ai_response
    current = SubElement(root, "current_turn")
    SubElement(current, "user_message").text = inputs.current_turn.user_message
    SubElement(current, "ai_response").text = inputs.current_turn.ai_response
    return _TEMPLATE.replace("{{dialogue_input}}", tostring(root, encoding="unicode"))
