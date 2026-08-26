"""이미지 프롬프트 조립(KNK-939).

컴파일 LLM이 생성한 외형 필드(JSON)를 이미지 프롬프트 템플릿의
<character> 블록에 끼워 완성한다. <character> 외의 블록은 전부 고정이다.

프롬프트 틀은 prompt/image/CHARACTER-IMAGE-TEMPLATE.md에 있다.
"""

import re
from pathlib import Path

from src.schemas.story_compile import CharacterSetting
from src.services.prompt_meta import read_version

# src/services/image/ → 프로젝트 루트까지 3단계 올라간다.
_TEMPLATE_PATH = Path(__file__).parent.parent.parent.parent / "prompt" / "image" / "CHARACTER-IMAGE-TEMPLATE.md"

# 버전은 이미지 프롬프트 frontmatter가 SSOT다. 컴파일 응답·관측 메타에 함께 싣는다.
CHARACTER_IMAGE_VERSION = read_version(_TEMPLATE_PATH)


def _load_template() -> str:
    """프롬프트 템플릿 파일에서 XML 본문을 꺼낸다.

    frontmatter와 제목·설명 줄을 건너뛰고 <image_prompt>부터 끝까지 가져온다.
    """
    try:
        raw = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise RuntimeError(f"이미지 프롬프트 템플릿을 찾을 수 없습니다: {_TEMPLATE_PATH}") from e

    # <image_prompt> 태그부터 끝까지 추출
    match = re.search(r"(<image_prompt>.*</image_prompt>)", raw, re.DOTALL)
    if not match:
        raise RuntimeError(f"이미지 프롬프트 템플릿에 <image_prompt> 블록이 없습니다: {_TEMPLATE_PATH}")
    return match.group(1)


# 모듈 로드 시 한 번 읽는다(컴파일 프롬프트와 같은 패턴).
_TEMPLATE = _load_template()


def build_image_prompt(character: CharacterSetting, genre_tags: list[str]) -> str | None:
    """인물 카드의 외형 필드와 장르 태그로 이미지 프롬프트를 조립한다.

    <character> 블록의 {{...}} 자리표시자를 교체한다.
    외형 필드가 하나라도 비어 있으면 None을 반환한다(이미지 생성 불가).
    """
    appearance = (
        character.age,
        character.body,
        character.face,
        character.hair,
        character.outfit,
        character.visual_identity,
    )
    if not all(value.strip() for value in appearance):
        return None

    mapping = {
        "genre": ", ".join(genre_tags),
        "gender": character.gender,
        "age": character.age,
        "body": character.body,
        "face": character.face,
        "hair": character.hair,
        "outfit": character.outfit,
        "visual_identity": character.visual_identity,
    }
    # 단일 패스 치환 (컴파일 프롬프트의 _render와 같은 원칙).
    pattern = r"\{\{(" + "|".join(map(re.escape, mapping)) + r")\}\}"
    return re.sub(pattern, lambda m: mapping[m.group(1)], _TEMPLATE)
