"""세분 명세(StorySpec) → 백엔드 계약(StoryCompileResponse) 변환.

LLM은 검증·재호출이 쉽도록 세분 JSON으로 답하고, 이 모듈이 그 결과를 사람이 보기 좋은
통글 마크다운 + ERD 4테이블 nested 형태로 재구성한다. 별도 템플릿 파일 없이 f-string으로
조립한다. genre는 백엔드가 입력 태그로 채우므로 여기서 제외한다.
"""

from src.schemas.story_compile import (
    CharacterSetting,
    PromptSettings,
    StoriesOut,
    StoryCompileResponse,
    StoryEndingOut,
    StoryMainEventOut,
    StorySettingsOut,
    StorySpec,
    StoryStartSettingsOut,
    UserRoleSetting,
)


def _render_world_setting(ps: PromptSettings) -> str:
    """STORY(무대) 통글 — 세계관 + 전제 + 갈등."""
    return (
        f"# 세계관\n{ps.world_setting}\n\n"
        f"# 전제\n{ps.plot_setting.premise}\n\n"
        f"# 갈등\n{ps.plot_setting.conflict}"
    )


def _render_character_setting(characters: list[CharacterSetting]) -> str:
    """CHARACTER 통글 — 인물 카드를 1명당 블록으로 반복."""
    blocks = [
        (
            f"## {c.name}\n"
            f"### 성격\n{c.personality}\n"
            f"### 말투\n{c.tone}\n"
            f"### 동기\n{c.motivation}\n"
            f"### 주인공을 대하는 태도\n{c.attitude_to_user}"
        )
        for c in characters
    ]
    return "# 등장인물\n\n" + "\n\n".join(blocks)


def _render_user_role_setting(ur: UserRoleSetting) -> str:
    """USER 통글 — 주인공 프로필. preference는 비어 있을 수 있다."""
    return (
        f"# 주인공\n"
        f"## 호칭\n{ur.name}\n"
        f"## 역할\n{ur.role}\n"
        f"## 배경\n{ur.background}\n"
        f"## 성격\n{ur.personality}\n"
        f"## 입력 선호\n{ur.preference}"
    )


def _render_rule_setting(ps: PromptSettings) -> str:
    """STORY(연출/출력) 통글 — 전개 규칙 + 문체 톤 + 분량 배분."""
    return (
        f"# 전개 규칙\n{ps.rule_setting}\n\n"
        f"# 문체 톤\n{ps.tone_setting}\n\n"
        f"# 분량 배분\n{ps.length_ratio}"
    )


def spec_to_response(spec: StorySpec) -> StoryCompileResponse:
    """세분 StorySpec을 ERD 4테이블 nested 계약(StoryCompileResponse)으로 변환한다."""
    ps = spec.prompt_settings
    return StoryCompileResponse(
        stories=StoriesOut(
            title=spec.meta.title,
            one_line_intro=spec.meta.one_line_intro,
            description=spec.meta.description,
        ),
        story_settings=StorySettingsOut(
            world_setting=_render_world_setting(ps),
            character_setting=_render_character_setting(ps.character_setting),
            user_role_setting=_render_user_role_setting(ps.user_role_setting),
            rule_setting=_render_rule_setting(ps),
        ),
        story_start_settings=StoryStartSettingsOut(
            name=spec.start.name,
            start_situation=spec.start.start_situation,
            prologue=spec.start.prologue,
        ),
        story_suggested_inputs=spec.suggested_inputs,
        story_main_events=[
            StoryMainEventOut(
                name=ev.name,
                description=ev.description,
                key_sentence=ev.key_sentence,
            )
            for ev in spec.main_events
        ],
        story_endings=[
            StoryEndingOut(
                name=e.name,
                min_turns=e.min_turns,
                achievement_condition=e.achievement_condition,
                epilogue=e.epilogue,
            )
            for e in spec.endings
        ],
    )
