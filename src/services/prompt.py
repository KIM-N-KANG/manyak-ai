from pathlib import Path

_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent / "prompt" / "story" / "STORY-PROMPT-TEMPLATE.md"
)

try:
    _template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    _, _after_system = _template.split("## [SYSTEM]", 1)
    _system_raw, _user_raw = _after_system.split("## [USER]", 1)
    _SYSTEM_TEXT = _system_raw.strip().removesuffix("---").strip()
    _USER_TEMPLATE = _user_raw.strip()
except (FileNotFoundError, ValueError) as e:
    raise RuntimeError(f"스토리 프롬프트 템플릿 로드 또는 파싱 실패: {e}")


def build_story_prompt(
    genre_tags: list[str],
    protagonist_tags: list[str],
    supporting_tags: list[str],
) -> tuple[str, str]:
    user_text = (
        _USER_TEMPLATE
        .replace("{{장르_태그}}", ", ".join(genre_tags))
        .replace("{{주인공_특징_태그}}", ", ".join(protagonist_tags))
        .replace("{{주변_인물_태그}}", ", ".join(supporting_tags))
    )
    return _SYSTEM_TEXT, user_text
