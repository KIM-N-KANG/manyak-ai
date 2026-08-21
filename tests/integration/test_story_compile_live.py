import os

import pytest

from src.schemas.story_compile import StoryCompileRequest, StoryCompileResponse
from src.services.story_llm import compile_story


@pytest.fixture(autouse=True)
def require_live_env() -> None:
    # 실제 LLM을 호출하므로 의도적으로 켤 때만 실행한다. CI는 더미 키를 주입하므로
    # "키 존재"로 판별하면 스킵되지 않고 실패한다 — 명시적 스위치로 옵트인한다.
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("RUN_LIVE_TESTS=1이 아니면 라이브 통합 테스트를 건너뜁니다")


async def test_compile_story_live() -> None:
    request = StoryCompileRequest(
        selected_storyline=(
            "역병과 반란으로 무너진 왕국에서, 견습 기사인 주인공이 선왕의 의문사를 둘러싼 "
            "진실에 다가갈수록 동료와 적의 경계가 흐려지는 선택의 기로에 선다."
        ),
        additional_info="주인공은 복수보다 진실을 택하는 신중한 성격이다.",
        genre_tags=["다크 판타지"],
        protagonist={"name": "카일", "gender": "MALE", "features": ["신중한", "관찰력 있는"]},
        supporting_characters=[
            {"name": "로한", "gender": "MALE", "features": ["충직한"]},
            {"features": ["계산적인", "거친"]},
        ],
    )

    res = await compile_story(request)

    # 구조 불변식만 검증 — 내용 품질(Layer 4)은 범위 밖
    assert isinstance(res, StoryCompileResponse)
    assert res.stories.title.strip()
    # story_settings 4필드는 통글 마크다운
    assert res.story_settings.world_setting.startswith("# 세계관")
    assert "## " in res.story_settings.character_setting  # 인물 카드 1명 이상
    assert res.story_settings.user_role_setting.strip()
    assert "# 분량 배분" in res.story_settings.rule_setting
    assert res.story_start_settings.prologue.strip()
    assert len(res.story_suggested_inputs) == 3

    # 주요 사건 3~5개(KNK-417/465 산출물) — 유닛은 mock이라 실 LLM만 이 계약을 관측한다.
    assert 3 <= len(res.story_main_events) <= 5
    assert all(ev.name.strip() and ev.key_sentence.strip() for ev in res.story_main_events)

    # 엔딩은 "0개(폴백) 또는 정확히 3개" 계약. 3개면 min_turns는 하한 1 이상.
    assert len(res.story_endings) in (0, 3)
    if res.story_endings:
        assert all(e.min_turns >= 1 for e in res.story_endings)
        assert all(e.achievement_condition.strip() and e.epilogue.strip() for e in res.story_endings)

    # 로깅 meta(KNK-243) — usage 토큰이 실제로 채워지는지는 라이브만 검증할 수 있다.
    assert res.meta is not None
    assert res.meta.input_token_count and res.meta.input_token_count > 0
    assert res.meta.output_token_count and res.meta.output_token_count > 0
