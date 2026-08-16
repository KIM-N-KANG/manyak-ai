from pathlib import Path

from src.schemas.story import CharacterInput
from src.schemas.story_compile import LorebookItem
from src.services.prompt_meta import read_version

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompt" / "story"
_STORYLINES_TEMPLATE_PATH = _PROMPT_DIR / "STORYLINES-TEMPLATE.md"
_COMPILE_TEMPLATE_PATH = _PROMPT_DIR / "COMPILE-TEMPLATE.md"

# 버전은 파일명이 아니라 frontmatter가 SSOT다(KNK-228). 로깅용으로 노출한다(KNK-243).
STORYLINES_VERSION = read_version(_STORYLINES_TEMPLATE_PATH)
COMPILE_VERSION = read_version(_COMPILE_TEMPLATE_PATH)


def _load_template(path: Path) -> tuple[str, str]:
    """템플릿 파일을 `[SYSTEM]` / `[USER]` 두 블록으로 분할해 반환한다."""
    try:
        template = path.read_text(encoding="utf-8")
        _, after_system = template.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
        return system_raw.strip().removesuffix("---").strip(), user_raw.strip()
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"프롬프트 템플릿 로드 또는 파싱 실패: {path.name}: {e}")


_STORYLINES_SYSTEM, _STORYLINES_USER = _load_template(_STORYLINES_TEMPLATE_PATH)
_COMPILE_SYSTEM, _COMPILE_USER = _load_template(_COMPILE_TEMPLATE_PATH)


def _all_features(characters: list[CharacterInput]) -> list[str]:
    return [f for c in characters for f in c.features]


def build_storylines_prompt(
    genre_tags: list[str],
    protagonist: CharacterInput,
    supporting_characters: list[CharacterInput],
) -> tuple[str, str]:
    # 인물 블록 치환(이름·성별 포함)은 KNK-836에서 템플릿과 함께 바꾼다.
    # 그전까지는 종전 자리표시자에 특징만 넣어 동작을 유지한다.
    user_text = (
        _STORYLINES_USER
        .replace("{{장르_태그}}", ", ".join(genre_tags))
        .replace("{{주인공_특징_태그}}", ", ".join(protagonist.features))
        .replace("{{주변_인물_태그}}", ", ".join(_all_features(supporting_characters)))
    )
    return _STORYLINES_SYSTEM, user_text


def _format_lorebooks(lorebooks: list[LorebookItem]) -> str:
    """로어북을 프롬프트 블록으로 만든다. 비어 있으면 `(없음)`(추가정보와 동일 관례)."""
    if not lorebooks:
        return "(없음)"
    # 이름·내용 앞뒤 공백·개행을 털어 ### 헤더·문단이 항상 깔끔히 렌더되게 한다.
    return "\n\n".join(f"### {lb.name.strip()}\n{lb.content.strip()}" for lb in lorebooks)


def build_compile_prompt(
    selected_storyline: str,
    additional_info: str,
    genre_tags: list[str],
    protagonist: CharacterInput,
    supporting_characters: list[CharacterInput],
    lorebooks: list[LorebookItem] | None = None,
) -> tuple[str, str]:
    """스토리 컴파일(시점 A-1)용 프롬프트를 완성한다."""
    # 인물 블록 치환(이름·성별 포함)은 KNK-837에서 템플릿과 함께 바꾼다(위와 동일).
    user_text = (
        _COMPILE_USER
        .replace("{{선택_스토리라인}}", selected_storyline)
        .replace("{{추가정보}}", additional_info or "(없음)")
        .replace("{{장르_태그}}", ", ".join(genre_tags))
        .replace("{{주인공_특징_태그}}", ", ".join(protagonist.features))
        .replace("{{주변_인물_태그}}", ", ".join(_all_features(supporting_characters)))
        .replace("{{로어북}}", _format_lorebooks(lorebooks or []))
    )
    return _COMPILE_SYSTEM, user_text


def build_refill_prompt(
    original_user_prompt: str,
    current_data_json: str,
    missing_blocks: list[str],
) -> tuple[str, str]:
    """누락·빈 블록만 다시 채우기 위한 부분 재호출 프롬프트를 만든다.

    직전 결과를 맥락으로 주고, 비어 있는 블록만 채워 그 블록만 키로 갖는 JSON을
    반환하도록 요청한다. 잘 나온 다른 블록은 보존하기 위해 응답에 포함하지 않게 한다.
    """
    blocks_str = ", ".join(missing_blocks)
    user_text = (
        f"{original_user_prompt}\n\n"
        f"--- 직전 생성 결과(JSON) ---\n{current_data_json}\n\n"
        f"위 결과에서 다음 블록이 비어 있거나 누락됐다: {blocks_str}.\n"
        f"이 블록들만 작성 규칙·스키마에 맞게 새로 채워서, **해당 블록만** 최상위 키로 "
        f"갖는 JSON 객체를 반환하라. 다른 블록은 절대 포함하지 말 것. "
        f"설명·머리말·코드 펜스 없이 JSON만 출력한다."
    )
    return _COMPILE_SYSTEM, user_text
