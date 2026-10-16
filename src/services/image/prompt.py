"""이미지 프롬프트 조립(KNK-939·KNK-1047·KNK-1484).

컴파일 LLM이 생성한 외형 필드(JSON)를 이미지 프롬프트 템플릿에 끼워 완성한다.

- 인물 이미지: prompt/image/CHARACTER-IMAGE-TEMPLATE.md — 자연어 `## 본문`에 인물 한 명의
  외형과, 요청이 고른 화풍의 `## 화풍: <값>` 블록을 끼운다(KNK-1484).
- 썸네일(표지): prompt/image/THUMBNAIL-IMAGE-TEMPLATE.md의 <characters> 블록에 인물 0~2명.

자리표시자 외의 본문은 전부 고정이다.
"""

import re
from pathlib import Path

from src.schemas.story_compile import IMAGE_ART_STYLES, CharacterSetting
from src.services.prompt_meta import read_version

# src/services/image/ → 프로젝트 루트까지 3단계 올라간다.
_PROMPT_DIR = Path(__file__).parent.parent.parent.parent / "prompt" / "image"
_TEMPLATE_PATH = _PROMPT_DIR / "CHARACTER-IMAGE-TEMPLATE.md"
_THUMBNAIL_TEMPLATE_PATH = _PROMPT_DIR / "THUMBNAIL-IMAGE-TEMPLATE.md"

# 버전은 이미지 프롬프트 frontmatter가 SSOT다. 컴파일 응답·관측 메타에 함께 싣는다.
CHARACTER_IMAGE_VERSION = read_version(_TEMPLATE_PATH)
THUMBNAIL_IMAGE_VERSION = read_version(_THUMBNAIL_TEMPLATE_PATH)

# 표지에 올리는 인물 수 상한. 셋 이상은 작은 썸네일에서 얼굴이 읽히지 않는다(KNK-1047).
THUMBNAIL_MAX_CHARACTERS = 2


def _load_template(path: Path) -> str:
    """프롬프트 템플릿 파일에서 XML 본문을 꺼낸다.

    frontmatter와 제목·설명 줄을 건너뛰고 <image_prompt>부터 끝까지 가져온다.
    같은 꼴의 다른 템플릿(썸네일 등)도 읽을 수 있게 경로를 인자로 받는다.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise RuntimeError(f"이미지 프롬프트 템플릿을 찾을 수 없습니다: {path}") from e

    # <image_prompt> 태그부터 끝까지 추출
    match = re.search(r"(<image_prompt>.*</image_prompt>)", raw, re.DOTALL)
    if not match:
        raise RuntimeError(f"이미지 프롬프트 템플릿에 <image_prompt> 블록이 없습니다: {path}")
    return match.group(1)


_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.M)
_CHARACTER_BODY_SECTION = "본문"
_CHARACTER_STYLE_PREFIX = "화풍: "


def _load_character_template(path: Path) -> tuple[str, dict[str, str]]:
    """인물 템플릿에서 자연어 본문과 화풍별 블록을 꺼낸다(KNK-1484).

    파일은 `## 본문` 하나와 `## 화풍: <값>` 여러 개로 나뉜다. 본문은 `{{art_style}}` 자리에
    화풍 블록을 끼워 완성된다. 그림을 만드는 화풍(IMAGE_ART_STYLES) 블록이 하나라도 없으면
    모듈 로드 시점에 실패시킨다 — 첫 컴파일 요청에서 KeyError로 드러나면 늦다.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise RuntimeError(f"이미지 프롬프트 템플릿을 찾을 수 없습니다: {path}") from e

    parts = _SECTION_RE.split(raw)  # [머리말, 제목1, 내용1, 제목2, 내용2, ...]
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    body = sections.get(_CHARACTER_BODY_SECTION)
    if not body:
        raise RuntimeError(f"인물 이미지 템플릿에 `## {_CHARACTER_BODY_SECTION}` 절이 없습니다: {path}")
    if "{{art_style}}" not in body:
        raise RuntimeError(f"인물 이미지 템플릿 본문에 {{{{art_style}}}} 자리표시자가 없습니다: {path}")
    styles = {
        title[len(_CHARACTER_STYLE_PREFIX):]: text
        for title, text in sections.items()
        if title.startswith(_CHARACTER_STYLE_PREFIX) and text
    }
    missing = [style for style in IMAGE_ART_STYLES if style not in styles]
    if missing:
        raise RuntimeError(f"인물 이미지 템플릿에 화풍 블록이 없습니다: {missing} ({path})")
    return body, styles


# 모듈 로드 시 한 번 읽는다(컴파일 프롬프트와 같은 패턴).
_TEMPLATE, _ART_STYLE_BLOCKS = _load_character_template(_TEMPLATE_PATH)
_THUMBNAIL_TEMPLATE = _load_template(_THUMBNAIL_TEMPLATE_PATH)
# 표지도 요청 화풍으로 그린다(KNK-1484). 화풍 블록은 인물 템플릿 한 곳에서만 정의하고 표지는 그것을 끼운다.
if "{{art_style}}" not in _THUMBNAIL_TEMPLATE:
    raise RuntimeError(f"썸네일 템플릿에 {{{{art_style}}}} 자리표시자가 없습니다: {_THUMBNAIL_TEMPLATE_PATH}")


def _render(template: str, mapping: dict[str, str]) -> str:
    """{{key}} 자리표시자를 단일 패스로 치환한다(컴파일 프롬프트의 _render와 같은 원칙).

    단일 패스라 치환한 값 안에 `{{`가 있어도 다시 치환되지 않는다.
    """
    pattern = r"\{\{(" + "|".join(map(re.escape, mapping)) + r")\}\}"
    return re.sub(pattern, lambda m: mapping[m.group(1)], template)


def has_full_appearance(character: CharacterSetting) -> bool:
    """외형 6필드가 모두 채워졌는지. 하나라도 비면 이미지 프롬프트를 만들 수 없다."""
    appearance = (
        character.age,
        character.body,
        character.face,
        character.hair,
        character.outfit,
        character.visual_identity,
    )
    return all(value.strip() for value in appearance)


def build_image_prompt(
    character: CharacterSetting, genre_tags: list[str], art_style: str
) -> str | None:
    """인물 카드의 외형 필드·장르 태그·화풍으로 이미지 프롬프트를 조립한다.

    본문의 {{...}} 자리표시자를 한 번에 교체한다. 화풍 블록도 같은 패스에 끼우므로 블록 안의
    글자가 다시 치환되지 않는다. 외형 필드가 하나라도 비어 있으면 None을 반환한다(이미지 생성 불가).
    art_style은 IMAGE_ART_STYLES 중 하나여야 한다 — 스키마가 막지만 코드 실수는 여기서 KeyError로 드러낸다.
    """
    if not has_full_appearance(character):
        return None

    mapping = {
        "art_style": _ART_STYLE_BLOCKS[art_style],
        "genre": ", ".join(genre_tags),
        "gender": character.gender,
        "age": character.age,
        "body": character.body,
        "face": character.face,
        "hair": character.hair,
        "outfit": character.outfit,
        "visual_identity": character.visual_identity,
    }
    return _render(_TEMPLATE, mapping)


def select_thumbnail_characters(characters: list[CharacterSetting]) -> list[CharacterSetting]:
    """표지에 올릴 인물을 고른다 — 외형이 모두 채워진 인물 중 카드 순서 앞 1~2명.

    주인공(UserRoleSetting)은 외형 필드가 없어 입력에 들어오지 않는다.
    외형이 다 찬 인물이 없으면 카드 첫 번째 인물을 넣는다 — 표지에는 인물이 최소 1명은
    있어야 한다(2026-08-31 결정). 빈 외형 칸은 _render_characters가 빼고 보낸다.
    인물 카드가 아예 없을 때만 빈 목록이다.
    """
    complete = [c for c in characters if has_full_appearance(c)][:THUMBNAIL_MAX_CHARACTERS]
    if complete or not characters:
        return complete
    return [characters[0]]


_CHARACTER_FIELDS = ("gender", "age", "body", "visual_identity", "face", "hair", "outfit")


def _render_characters(characters: list[CharacterSetting]) -> str:
    """<characters> 블록 안에 들어갈 본문. 인물 카드가 아예 없으면 템플릿이 약속한 `없음` 한 줄.

    값이 빈 칸은 태그째 뺀다 — 빈 태그를 보내면 모델이 "없는 특징"으로 읽을 수 있다.
    """
    if not characters:
        return "    없음"

    blocks = []
    for c in characters:
        lines = ["    <character>"]
        for field in _CHARACTER_FIELDS:
            value = getattr(c, field).strip()
            if value:
                lines.append(f"      <{field}>{value}</{field}>")
        lines.append("    </character>")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def build_thumbnail_prompt(
    characters: list[CharacterSetting], genre_tags: list[str], art_style: str
) -> str:
    """장르 태그·인물 카드 전체·화풍으로 썸네일(표지) 프롬프트를 조립한다.

    art_style은 인물 이미지와 같은 IMAGE_ART_STYLES 값이다 — 표지와 인물 그림이 한 작품처럼 보여야 한다.
    NONE이면 호출부(compile_story)가 표지를 만들지 않으므로 여기로 오지 않는다.

    표지 인물은 여기서 select_thumbnail_characters로 고른다 — 호출부는 컴파일 인물
    전원을 그대로 넘긴다. 인물 이름은 싣지 않는다(표지에 글자를 넣지 않고, 이름은
    그림에 도움이 안 된다). 인물이 없어도 항상 문자열을 돌려준다(장르 장면 표지).
    """
    mapping = {
        "art_style": _ART_STYLE_BLOCKS[art_style],
        "genre": ", ".join(genre_tags),
        "characters": _render_characters(select_thumbnail_characters(characters)),
    }
    return _render(_THUMBNAIL_TEMPLATE, mapping)
