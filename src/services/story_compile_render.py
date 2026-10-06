"""세분 명세(StorySpec) → 백엔드 계약(StoryCompileResponse) 변환.

LLM은 검증·재호출이 쉽도록 세분 JSON으로 답하고, 이 모듈이 그 결과를 사람이 보기 좋은
통글 마크다운 + ERD 4테이블 nested 형태로 재구성한다. 별도 템플릿 파일 없이 f-string으로
조립한다. genre는 백엔드가 입력 태그로 채우므로 여기서 제외한다.
"""

import re

from src.schemas.story_compile import (
    CharacterAppearanceOut,
    CharacterIntroductionOut,
    CharacterSetting,
    PromptSettings,
    StoriesOut,
    StoryCompileResponse,
    StoryEndingOut,
    StoryMainEventOut,
    StorySettingsOut,
    StorySpec,
    StoryStartSettingsOut,
    ThumbnailImageOut,
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
            f"### 성별\n{c.gender}\n"
            f"### 성격\n{c.personality}\n"
            f"### 말투\n{c.tone}\n"
            f"### 동기\n{c.motivation}\n"
            f"### 주인공을 대하는 태도\n{c.attitude_to_user}"
        )
        for c in characters
    ]
    return "# 등장인물\n\n" + "\n\n".join(blocks)


def _render_user_role_setting(ur: UserRoleSetting) -> str:
    """USER 통글 — 이름을 제외한 주인공 프로필. preference는 비어 있을 수 있다."""
    return (
        f"# 주인공\n"
        f"## 성별\n{ur.gender}\n"
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


def _render_character_appearances(
    characters: list[CharacterSetting],
) -> list[CharacterAppearanceOut]:
    """인물별 외형 정보를 응답용 모델로 변환한다.

    컴파일 LLM이 생성한 외형 필드를 백엔드가 DB에 저장할 수 있도록 별도 배열로 내려준다.
    통글 마크다운(character_setting)에는 포함되지 않는 보조 데이터다.
    """
    return [
        CharacterAppearanceOut(
            name=c.name,
            gender=c.gender,
            age=c.age,
            body=c.body,
            face=c.face,
            hair=c.hair,
            outfit=c.outfit,
            visual_identity=c.visual_identity,
        )
        for c in characters
    ]


_JOSA = {
    "은": "은(는)", "는": "은(는)", "이": "이(가)", "가": "이(가)",
    "을": "을(를)", "를": "을(를)", "과": "과(와)", "와": "과(와)",
    "으로": "으로(로)", "로": "으로(로)", "아": "아(야)", "야": "아(야)",
    "이랑": "이랑(랑)", "랑": "이랑(랑)",
}


def _tokenize_name(text: str, name: str) -> str:
    """이름을 치환하고, 단독으로 붙은 흔한 조사만 백엔드 치환 표기로 바꾼다."""
    if not name:
        return text
    particles = "|".join(sorted(_JOSA, key=len, reverse=True))
    pattern = re.escape(name) + rf"(?:(?P<josa>{particles})(?!\w|\())?"
    return re.sub(
        pattern,
        lambda match: "{username}" + _JOSA.get(match.group("josa"), ""),
        text,
    )


def spec_to_response(spec: StorySpec, *, thumbnail_image: ThumbnailImageOut) -> StoryCompileResponse:
    """세분 StorySpec을 ERD 4테이블 nested 계약(StoryCompileResponse)으로 변환한다.

    thumbnail_image는 필수 응답 필드라 호출부가 실제 결과를 넘긴다 — 임시값을 여기서 만들면
    그 값이 응답으로 새어 나갈 수 있다(KNK-1047).
    """
    ps = spec.prompt_settings

    def prose(text: str) -> str:
        return _tokenize_name(text, ps.user_role_setting.name)

    return StoryCompileResponse(
        stories=StoriesOut(
            title=prose(spec.meta.title),
            one_line_intro=prose(spec.meta.one_line_intro),
            description=prose(spec.meta.description),
        ),
        story_settings=StorySettingsOut(
            protagonist_name=ps.user_role_setting.name,
            world_setting=prose(_render_world_setting(ps)),
            character_setting=prose(_render_character_setting(ps.character_setting)),
            user_role_setting=prose(_render_user_role_setting(ps.user_role_setting)),
            rule_setting=prose(_render_rule_setting(ps)),
        ),
        story_start_settings=StoryStartSettingsOut(
            name=prose(spec.start.name),
            start_situation=prose(spec.start.start_situation),
            prologue=prose(spec.start.prologue),
        ),
        story_suggested_inputs=[prose(text) for text in spec.suggested_inputs],
        story_main_events=[
            StoryMainEventOut(
                name=prose(ev.name),
                description=prose(ev.description),
                key_sentence=prose(ev.key_sentence),
            )
            for ev in spec.main_events
        ],
        story_endings=[
            StoryEndingOut(
                name=prose(e.name),
                min_turns=e.min_turns,
                achievement_condition=prose(e.achievement_condition),
                epilogue=prose(e.epilogue),
            )
            for e in spec.endings
        ],
        character_introductions=[
            CharacterIntroductionOut(name=c.name, description=prose(c.description))
            for c in ps.character_setting
        ],
        character_appearances=_render_character_appearances(ps.character_setting),
        thumbnail_image=thumbnail_image,
    )
