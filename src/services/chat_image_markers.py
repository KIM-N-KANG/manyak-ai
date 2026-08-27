"""저장용 인물 이미지 마커를 LLM 입력 복사본에서 제거한다."""

import re


# 마커 뒤의 줄바꿈(최대 2개)도 함께 지운다 — 마커는 대사 줄 위에 빈 줄을 두고 따로 저장되므로
# (KNK-1002) 마커만 지우면 LLM 입력에 빈 줄이 남는다. 줄바꿈 없이 대사 옆에 붙은 옛 모양의
# 마커(개발 서버에 남은 기록)도 같은 식으로 지워진다.
# 옛 `[character:이름]` 태그는 더 지우지 않는다(KNK-1007) — 이미지 근거가 인물명 라벨로
# 바뀌어 LLM이 태그를 출력하지 않으므로, 남은 것은 개발 서버 옛 기록뿐이다.
_STORAGE_MARKER_RE = re.compile(r"\[\[[^:\]\r\n]+:[^\]\r\n]+\]\](?:\r?\n){0,2}")


def strip_character_image_syntax(text: str) -> str:
    """저장 마커만 제거하고 나머지 본문은 보존한다."""
    return _STORAGE_MARKER_RE.sub("", text)
