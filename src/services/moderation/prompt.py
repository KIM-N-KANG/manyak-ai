"""검수 템플릿에 게시물과 경로별 실제 이미지를 넣는다."""

import json
from functools import lru_cache
from pathlib import Path

from src.services.llm.base import ContentPart, Message, text_part
from src.services.moderation.images import ModerationImage
from src.services.moderation.input import ModerationInput

_PATH = Path(__file__).resolve().parents[3] / "prompt/moderation/MODERATION-TEMPLATE.md"


@lru_cache(maxsize=1)
def _template() -> tuple[str, str]:
    raw = _PATH.read_text(encoding="utf-8-sig")
    _, system_marker, body = raw.partition("## [SYSTEM]")
    system, user_marker, user = body.partition("## [USER]")
    if not system_marker or not user_marker or user.count("{post_json}") != 1:
        raise RuntimeError("검수 템플릿의 SYSTEM·USER 구분 또는 입력 자리표시자 오류")
    return system.strip(), user.strip()


def build_messages(inputs: ModerationInput, images: list[ModerationImage]) -> list[Message]:
    system, user = _template()
    # JSON 안의 '<'와 '>'를 이스케이프해 입력이 XML 구분자를 닫지 못하게 한다.
    post_json = json.dumps(inputs.post, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    parts: list[ContentPart] = [text_part(user.replace("{post_json}", post_json))]
    for image in images:
        parts.extend([text_part(f"첨부 이미지 경로: {image.path}"), image.content_part()])
    return [{"role": "system", "content": system}, {"role": "user", "content": parts}]
