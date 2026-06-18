from pathlib import Path

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompt" / "story"
_STORY_TEMPLATE_PATH = _PROMPT_DIR / "STORY-PROMPT-TEMPLATE.md"
_COMPILE_TEMPLATE_PATH = _PROMPT_DIR / "STORY-COMPILE-TEMPLATE.md"


def _load_template(path: Path) -> tuple[str, str]:
    """템플릿 파일을 `[SYSTEM]` / `[USER]` 두 블록으로 분할해 반환한다."""
    try:
        template = path.read_text(encoding="utf-8")
        _, after_system = template.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
        return system_raw.strip().removesuffix("---").strip(), user_raw.strip()
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"프롬프트 템플릿 로드 또는 파싱 실패: {path.name}: {e}")


_STORY_SYSTEM, _STORY_USER = _load_template(_STORY_TEMPLATE_PATH)
_COMPILE_SYSTEM, _COMPILE_USER = _load_template(_COMPILE_TEMPLATE_PATH)


def build_story_prompt(
    genre_tags: list[str],
    protagonist_tags: list[str],
    supporting_tags: list[str],
) -> tuple[str, str]:
    user_text = (
        _STORY_USER
        .replace("{{장르_태그}}", ", ".join(genre_tags))
        .replace("{{주인공_특징_태그}}", ", ".join(protagonist_tags))
        .replace("{{주변_인물_태그}}", ", ".join(supporting_tags))
    )
    return _STORY_SYSTEM, user_text


def build_compile_prompt(
    selected_storyline: str,
    extra_info: str,
    genre_tags: list[str],
    protagonist_tags: list[str],
    supporting_tags: list[str],
) -> tuple[str, str]:
    """스토리 컴파일(시점 A-1)용 프롬프트를 완성한다."""
    user_text = (
        _COMPILE_USER
        .replace("{{선택_스토리라인}}", selected_storyline)
        .replace("{{추가정보}}", extra_info or "(없음)")
        .replace("{{장르_태그}}", ", ".join(genre_tags))
        .replace("{{주인공_특징_태그}}", ", ".join(protagonist_tags))
        .replace("{{주변_인물_태그}}", ", ".join(supporting_tags))
    )
    return _COMPILE_SYSTEM, user_text
