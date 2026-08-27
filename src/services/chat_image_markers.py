"""저장용 인물 이미지 표시를 LLM 입력 복사본에서 제거한다."""

import re


# 마커 뒤의 줄바꿈(최대 2개)도 함께 지운다 — 마커는 대사 줄 위에 빈 줄을 두고 따로 저장되므로
# (KNK-1002) 마커만 지우면 LLM 입력에 빈 줄이 남는다. 줄바꿈 없이 대사 옆에 붙은 옛 모양의
# 마커(개발 서버에 남은 기록)도 같은 식으로 지워진다.
_STORAGE_MARKER_RE = re.compile(r"\[\[[^:\]\r\n]+:[^\]\r\n]+\]\](?:\r?\n){0,2}")
_RAW_CHARACTER_TAG_RE = re.compile(r"\[character:[^\]\r\n]*\]")


def strip_character_image_syntax(text: str) -> str:
    """URL 저장 마커와 노출된 원본 태그만 제거하고 나머지 본문은 보존한다."""
    without_markers = _STORAGE_MARKER_RE.sub("", text)
    return _RAW_CHARACTER_TAG_RE.sub("", without_markers)
