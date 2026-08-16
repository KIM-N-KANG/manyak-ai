from pydantic import BaseModel, Field, field_validator

from src.schemas.response_meta import StoryResponseMeta
from src.schemas.story import CharacterInput, SupportingCharacters


class LorebookItem(BaseModel):
    """장르 공용 용어 사전 한 항목 — 백엔드가 스토리 장르로 선별해 전달(§5-3-3).

    세계관·용어 확장의 재료로만 쓰고, 원문을 출력 계약에 그대로 노출하지 않는다.
    """

    name: str
    content: str


class StoryCompileRequest(BaseModel):
    """스토리 컴파일(시점 A-1) 입력 — 희소 입력.

    백엔드가 보내는 선택 스토리라인 1편 + 추가정보 + 장르 태그 + 인물 세트(KNK-833).
    """

    selected_storyline: str
    additional_info: str = ""
    genre_tags: list[str]
    protagonist: CharacterInput
    supporting_characters: SupportingCharacters = Field(default_factory=list)
    # 장르 공용 로어북(선택) — 미전달·빈 배열·null이면 프롬프트 미주입, 기존 요청과 하위호환(KNK-422).
    # 명시적 null도 "없음"으로 받도록 | None 허용(빌더는 `lorebooks or []`로 안전 처리).
    lorebooks: list[LorebookItem] | None = Field(default_factory=list)


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
    # 주요 인물 1~5명 카드화 — 나머지는 world_setting 배경으로 흡수
    character_setting: list[CharacterSetting] = Field(min_length=1, max_length=5)
    user_role_setting: UserRoleSetting


class Start(BaseModel):
    """세션 시작 화면 + 첫 턴 History 시드 재료. 정적 레이어 아님."""

    name: str
    prologue: str
    start_situation: str


class MainEvent(BaseModel):
    """주요 사건 — 이야기의 갈림길. key_sentence는 사용자 입력이 이 사건과 연결되는지 판단하는 기준."""

    name: str
    description: str
    key_sentence: str


class Ending(BaseModel):
    """엔딩 정의. 본문이 아니라 도달 시 생성될 에필로그의 이름·최소 턴·달성 조건·연출 방향.

    유형(해피/노말/배드)은 생성용 내부 기준일 뿐 계약엔 없고, 엔딩은 name으로 식별한다(KNK-465).
    """

    name: str
    min_turns: int = Field(ge=1)  # 최소 턴 문턱 — 0·음수면 첫 턴 도달이 가능해져 계약 위반(KNK-465)
    achievement_condition: str
    epilogue: str


class StorySpec(BaseModel):
    """스토리 컴파일(시점 A-1)의 영속 산출물 — 스토리 명세 JSON(MVP 확정본).

    spec/story/2-COMPILE.md §5-2(내부 세분 스키마)와 1:1 대응.
    """

    meta: Meta
    prompt_settings: PromptSettings
    start: Start
    suggested_inputs: list[str] = Field(min_length=3, max_length=3)
    # 주요 사건 3~5개를 결속 생성. 엔딩은 정상 3개이되, 재호출 후에도 못 채우면 빈 배열 폴백(KNK-465).
    main_events: list[MainEvent] = Field(min_length=3, max_length=5)
    endings: list[Ending] = Field(default_factory=list)

    @field_validator("endings")
    @classmethod
    def _zero_or_three(cls, v: list[Ending]) -> list[Ending]:
        """엔딩은 0개(폴백) 또는 정확히 3개다 — 그 사이 개수는 계약 위반."""
        if len(v) not in (0, 3):
            raise ValueError("endings must be empty (fallback) or exactly 3")
        return v


# ── 컴파일 API output (백엔드 계약) ─────────────────────────────────────────
# 내부 세분 스키마(StorySpec)를 ERD 4테이블에 1:1 대응하는 nested 형태로 재구성한 것.
# story_settings 4필드는 사람이 읽기 좋은 통글 마크다운, 나머지는 값 그대로 전달한다.


class StoriesOut(BaseModel):
    """노출 메타(stories 테이블). genre는 백엔드가 입력 태그로 채우므로 제외한다."""

    title: str
    one_line_intro: str
    description: str


class StorySettingsOut(BaseModel):
    """AI 프롬프트 재료(story_settings 테이블) — 통글 마크다운 4필드."""

    world_setting: str
    character_setting: str
    user_role_setting: str
    rule_setting: str


class StoryStartSettingsOut(BaseModel):
    """시작 설정(story_start_settings 테이블)."""

    name: str
    start_situation: str
    prologue: str


class StoryMainEventOut(BaseModel):
    """주요 사건(story_main_events 테이블) — 항목별 이산 필드로 전달(통글 아님). 배열 순서=명목 순서(비강제)."""

    name: str
    description: str
    key_sentence: str


class StoryEndingOut(BaseModel):
    """엔딩(story_endings 테이블) — 이름 기반 계약. 백엔드가 칸별로 저장한다(KNK-465)."""

    name: str
    min_turns: int = Field(ge=1)  # 최소 턴 문턱 하한(KNK-465)
    achievement_condition: str
    epilogue: str


class StoryCompileResponse(BaseModel):
    """컴파일 API output — ERD 테이블에 1:1 대응하는 nested 계약본."""

    stories: StoriesOut
    story_settings: StorySettingsOut
    story_start_settings: StoryStartSettingsOut
    story_suggested_inputs: list[str] = Field(min_length=3, max_length=3)
    story_main_events: list[StoryMainEventOut] = Field(min_length=3, max_length=5)  # 주요 사건 3~5개(KNK-417)
    story_endings: list[StoryEndingOut] = Field(default_factory=list)  # 0개(폴백) 또는 3개(KNK-465)
    meta: StoryResponseMeta | None = None  # 로깅 메타(KNK-243). compile_story가 항상 채운다.
