"""기존 요약과 대화 원문을 요약 모델 입력으로 조립한다."""

import json
from pathlib import Path

from src.schemas.chat_compact import ChatCompactRequest
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.llm.base import Message
from src.services.prompt_meta import read_version

_PATH = Path(__file__).resolve().parents[2] / "prompt/chat/COMPACT-TEMPLATE.md"
COMPACT_VERSION = read_version(_PATH)


def _load_template(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8-sig")
    _, system_marker, body = raw.partition("## [SYSTEM]")
    system, user_marker, user = body.partition("## [USER]")
    if not system_marker or not user_marker or not system.strip() or user.count("{compact_json}") != 1:
        raise RuntimeError("요약 템플릿의 SYSTEM·USER 구분 또는 입력 자리표시자 오류")
    return system.strip(), user.strip()


_SYSTEM, _USER_TEMPLATE = _load_template(_PATH)


def build_messages(request: ChatCompactRequest) -> list[Message]:
    """원본을 수정하지 않고 화자와 순서를 보존한 입력 복사본을 만든다."""
    payload = {
        "summary": strip_character_image_syntax(request.summary),
        "history": [
            {"role": item.role, "content": strip_character_image_syntax(item.content)}
            for item in request.history
        ],
    }
    # 한 번만 삽입하므로 사용자 입력의 자리표시자를 다시 치환하지 않는다.
    user = _USER_TEMPLATE.replace("{compact_json}", json.dumps(payload, ensure_ascii=False))
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
