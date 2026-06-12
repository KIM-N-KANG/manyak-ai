from pathlib import Path

_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent / "prompt" / "story" / "STORY-PROMPT-TEMPLATE.md"
)


def build_story_prompt(
    genre_tags: list[str],
    protagonist_tags: list[str],
    supporting_tags: list[str],
) -> tuple[str, str]:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    _, after_system = template.split("## [SYSTEM]", 1)
    system_raw, user_raw = after_system.split("## [USER]", 1)

    system_text = system_raw.strip().removesuffix("---").strip()
    user_text = (
        user_raw.strip()
        .replace("{{장르_태그}}", ", ".join(genre_tags))
        .replace("{{주인공_특징_태그}}", ", ".join(protagonist_tags))
        .replace("{{주변_인물_태그}}", ", ".join(supporting_tags))
    )

    return system_text, user_text
