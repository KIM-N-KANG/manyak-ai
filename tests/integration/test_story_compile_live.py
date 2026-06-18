import os

import pytest

from src.schemas.story_compile import StoryCompileRequest, StorySpec
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
        extra_info="주인공은 복수보다 진실을 택하는 신중한 성격이다.",
        genre_tags=["다크 판타지"],
        protagonist_tags=["신중한", "관찰력 있는"],
        supporting_tags=["충직한", "계산적인", "거친"],
    )

    spec = await compile_story(request)

    # 구조 불변식만 검증 — 내용 품질(Layer 4)은 범위 밖
    assert isinstance(spec, StorySpec)
    assert spec.meta.genre == "다크 판타지"  # genre 주입 정합
    assert 1 <= len(spec.prompt_settings.character_setting) <= 3
    assert spec.prompt_settings.world_setting.strip()
    assert spec.prompt_settings.user_role_setting.role.strip()
    assert spec.start.prologue.strip()
    assert len(spec.suggested_inputs) <= 3
