"""게시물 검수 API 입력. 작성 규칙은 백엔드가 검사하고 AI는 전달받은 내용만 검수한다."""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from src.services.moderation.models import ModerationResult


class _ModerationInput(BaseModel):
    # 백엔드의 ID·공개 설정 같은 검수 외 필드는 버린다. 잘못된 필드 타입은 변환하지 않는다.
    model_config = ConfigDict(strict=True, alias_generator=to_camel, extra="ignore")


class ModerationStorySettings(_ModerationInput):
    world_setting: str | None = None
    character_setting: str | None = None
    user_role_setting: str | None = None
    rule_setting: str | None = None


class ModerationEndingRequirement(_ModerationInput):
    achievement_condition: str | None = None


class ModerationEnding(_ModerationInput):
    name: str | None = None
    requirement: ModerationEndingRequirement | None = None
    epilogue: str | None = None


class ModerationStartSetting(_ModerationInput):
    name: str | None = None
    prologue: str | None = None
    start_situation: str | None = None
    suggested_inputs: list[str] | None = None
    endings: list[ModerationEnding] | None = None


class ModerationMainEvent(_ModerationInput):
    name: str | None = None
    description: str | None = None
    key_sentence: str | None = None


class ModerationCharacterImage(_ModerationInput):
    image_name: str | None = None
    # 접근 가능 여부·허용 호스트는 이미지 준비 단계에서 검사해 IMAGE_DOWNLOAD_FAILED로 반환한다.
    image_url: str | None = None


class ModerationCharacter(_ModerationInput):
    name: str | None = None
    images: list[ModerationCharacterImage] | None = None


class StoryModerationRequest(_ModerationInput):
    story_id: str
    title: str | None = None
    one_line_intro: str | None = None
    description: str | None = None
    genres: list[str] | None = None
    story_settings: ModerationStorySettings | None = None
    start_settings: list[ModerationStartSetting] | None = None
    main_events: list[ModerationMainEvent] | None = None
    thumbnail_url: str | None = None
    characters: list[ModerationCharacter] | None = None


class StoryModerationResponse(ModerationResult):
    """decision·issues·error_code·error_path 순서와 null 필드를 유지한다."""
