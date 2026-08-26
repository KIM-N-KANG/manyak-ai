"""저장용 인물 이미지 표시를 LLM 입력 복사본에서 제거한다."""

import re


_STORAGE_MARKER_RE = re.compile(r"\[\[[^:\]\r\n]+:[^\]\r\n]+\]\]")
_RAW_CHARACTER_TAG_RE = re.compile(r"\[character:[^\]\r\n]*\]")


def strip_character_image_syntax(text: str) -> str:
    """URL 저장 마커와 노출된 원본 태그만 제거하고 나머지 본문은 보존한다."""
    without_markers = _STORAGE_MARKER_RE.sub("", text)
    return _RAW_CHARACTER_TAG_RE.sub("", without_markers)
