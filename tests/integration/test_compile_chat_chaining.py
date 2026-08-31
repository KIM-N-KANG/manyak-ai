"""compile 산출물 → chat 조립 체이닝 테스트 (KNK-574 감사 2장 1번).

이 서비스의 핵심 데이터 흐름은 "컴파일이 만든 통글(world_setting 등)을 채팅 조립기가
슬롯에 소비"하는 것인데, 그동안 story 테스트와 chat 테스트가 완전히 분리돼 이 이음매를
확인하는 테스트가 0건이었다. 여기서는 LLM 없이(외부 IO 0) 기존 spec_valid 픽스처를
spec_to_response로 렌더한 뒤, 그 출력을 그대로 다음 채팅 턴 입력으로 이어 assemble()까지
흘려, 컴파일 통글이 올바른 프롬프트 슬롯에 들어갔는지 단언한다. 유닛 속도로 돈다.
"""

import json
from pathlib import Path

from src.schemas.chat_turn import (
    ChatStartSettings,
    ChatStorySettings,
    ChatTurnRequest,
    EndingCandidate,
    MainEvent,
)
from src.schemas.story_compile import StoryCompileResponse, StorySpec, ThumbnailImageOut
from src.services.chat_assembler import assemble
from src.services.story_compile_render import spec_to_response

_FIXTURES = Path(__file__).parent.parent / "unit" / "fixtures"


def _compiled() -> StoryCompileResponse:
    spec = StorySpec(**json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8-sig")))
    return spec_to_response(spec, thumbnail_image=ThumbnailImageOut(error="generation_failed"))


def _chat_request_from(res: StoryCompileResponse) -> ChatTurnRequest:
    """compile 산출물을 그대로 다음 채팅 턴 입력으로 잇는다(백엔드 배선 모사).

    story_settings·start_settings 통글은 필드 구조가 동일해 그대로 전달되고, endings는
    min_turns를 뺀 EndingCandidate로 싣는다(백엔드가 하한 충족분만 걸러 싣는 계약).
    genre는 stories.genre가 아니라 입력 태그에서 온다(명세 3.3 예외 경로)라 직접 채운다.
    """
    return ChatTurnRequest(
        genre="다크 판타지",
        story_settings=ChatStorySettings(
            world_setting=res.story_settings.world_setting,
            character_setting=res.story_settings.character_setting,
            user_role_setting=res.story_settings.user_role_setting,
            rule_setting=res.story_settings.rule_setting,
        ),
        start_settings=ChatStartSettings(
            name=res.story_start_settings.name,
            prologue=res.story_start_settings.prologue,
            start_situation=res.story_start_settings.start_situation,
        ),
        history=[],
        user_input="레이에게 문을 열어준다",
        summary="",
        main_events=[
            MainEvent(name=e.name, description=e.description, key_sentence=e.key_sentence)
            for e in res.story_main_events
        ],
        endings=[
            EndingCandidate(
                name=e.name, achievement_condition=e.achievement_condition, epilogue=e.epilogue
            )
            for e in res.story_endings
        ],
    )


def test_compiled_settings_flow_into_chat_slots() -> None:
    res = _compiled()
    system_front = assemble(_chat_request_from(res))[0]["content"]

    # 컴파일이 만든 4개 통글이 통째로 시스템 프롬프트 슬롯에 들어간다(문자열 등가).
    assert res.story_settings.world_setting in system_front  # STORY {{world_setting}}
    assert res.story_settings.character_setting in system_front  # CHARACTER {{character_setting}}
    assert res.story_settings.user_role_setting in system_front  # USER {{user_role_setting}}
    # 통글 헤더가 올바른 슬롯에 렌더됐는지(렌더러 f-string 산출물).
    assert "# 세계관" in system_front and "# 전제" in system_front and "# 갈등" in system_front
    assert "## 레이" in system_front  # 인물 카드 헤더
    assert "## 호칭" in system_front  # 주인공 프로필 헤더
    # 시작 설정은 name+prologue+start_situation 통글로 STORY {{start_setting}}에.
    assert "# 시작 설정: 선왕의 장례식 날" in system_front
    assert res.story_start_settings.prologue in system_front


def test_compiled_events_and_endings_flow_into_chat_slots() -> None:
    res = _compiled()
    system_front = assemble(_chat_request_from(res))[0]["content"]

    # 주요 사건 통글: '- 이름: {name} / 설명: … / 키 문장: …' (STORY {{main_events}})
    assert "선왕의 마지막 밤" in system_front
    assert "키 문장:" in system_front
    # 엔딩 통글: '- 이름: {name} / 달성 조건: … / 에필로그 가이드: …' (STORY {{endings}})
    assert "질서를 바로 세우다" in system_front
    assert "에필로그 가이드:" in system_front
    # 조립 결과에 미치환 슬롯이 남지 않는다(이음매 전체 결정성).
    assert "{{" not in system_front
