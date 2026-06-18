from pydantic import BaseModel, Field


class StoryCompileRequest(BaseModel):
    """스토리 컴파일(시점 A-1) 입력 — 희소 입력.

    백엔드가 보내는 선택 스토리라인 1편 + 추가정보 + 원본 태그.
    """

    selected_storyline: str
    extra_info: str = ""
    genre_tags: list[str]
    protagonist_tags: list[str]
    supporting_tags: list[str]


class Meta(BaseModel):
    """노출 메타. genre만 프롬프트(STORY)로 유입되고 나머지는 노출 전용."""

    title: str
    one_line_intro: str
    description: str
    genre: str


class PlotSetting(BaseModel):
    """STORY — 사건 설정. premise=핵심 전제(도입), conflict=갈등 가능성(Possibility)."""

    premise: str
    conflict: str


class CharacterSetting(BaseModel):
    """CHARACTER — 주변 인물 카드. 주인공은 포함하지 않는다(USER 소유)."""

    name: str
    personality: str
    tone: str
    motivation: str
    attitude_to_user: str


class UserRoleSetting(BaseModel):
    """USER — 주인공(1인칭 플레이어) 프로필."""

    name: str
    role: str
    background: str
    personality: str
    preference: str = ""


class PromptSettings(BaseModel):
    """STORY·CHARACTER·USER 세 정적 레이어의 슬롯 재료(`story_settings` 7필드)."""

    world_setting: str
    plot_setting: PlotSetting
    rule_setting: str
    tone_setting: str
    length_ratio: str
    # 주요 인물 최대 3명만 카드화 — 나머지는 world_setting 배경으로 흡수
    character_setting: list[CharacterSetting] = Field(max_length=3)
    user_role_setting: UserRoleSetting


class Start(BaseModel):
    """세션 시작 화면 + 첫 턴 History 시드 재료. 정적 레이어 아님."""

    name: str
    prologue: str
    start_situation: str


class StorySpec(BaseModel):
    """스토리 컴파일(시점 A-1)의 영속 산출물 — 스토리 명세 JSON(MVP 확정본).

    reference/chat/4-SERVICE-IMPLEMENTATION.md 3.4 스키마와 1:1 대응.
    """

    meta: Meta
    prompt_settings: PromptSettings
    start: Start
    suggested_inputs: list[str] = Field(max_length=3)
