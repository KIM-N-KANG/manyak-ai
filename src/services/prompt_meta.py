"""프롬프트 frontmatter에서 메타데이터(version)를 읽는다.

버전은 파일명이 아니라 각 프롬프트 상단 frontmatter의 `version`이 SSOT다(KNK-228).
로깅(KNK-243)에서 API 응답에 실어 백엔드 `ai_call_logs.prompt_template_version`을
채운다. 파일명을 고정해 버전이 바뀌어도 코드를 건드리지 않게 한다.
"""

import re
from pathlib import Path

# 파일 맨 앞 frontmatter 블록(--- ... ---). LF/CRLF 모두 허용한다.
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.S)
# frontmatter 안의 최상위 `version: <정수>` 줄.
_VERSION_RE = re.compile(r"^version:\s*(\d+)\s*$", re.M)


def read_version(path: Path) -> int:
    """프롬프트 상단 frontmatter의 `version`(정수)을 읽는다.

    frontmatter 블록이나 `version` 줄이 없으면 RuntimeError를 던진다 — 버전 누락은
    로깅 메타가 비는 것이라 조용히 넘기지 않고 모듈 로드 시점에 드러낸다.
    """
    text = path.read_text(encoding="utf-8-sig")  # BOM 있으면 떼고 읽는다(없으면 utf-8과 동일)
    block = _FRONTMATTER_RE.match(text)
    if not block:
        raise RuntimeError(f"frontmatter 블록 없음: {path.name}")
    version = _VERSION_RE.search(block.group(1))
    if not version:
        raise RuntimeError(f"frontmatter version 누락: {path.name}")
    return int(version.group(1))
