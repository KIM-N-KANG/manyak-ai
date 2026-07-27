"""응답 메타 스키마의 계약 가드 (KNK-674 2차 리뷰 6번).

`provider`는 **반드시 채워 넣는 값**이다. 기본값을 두면 새 조립 자리가 provider를 빠뜨려도
조용히 그 값을 물려받아, 이번 티켓이 없앤 전역 폴백(`settings.llm_provider`)이 이름만 바꿔
되살아난다. 값이 틀린 기록은 없는 기록보다 나쁘다 — 다른 회사로 나간 호출이 전부 한 공급자
탓으로 보인다.

dataclass 두 곳(`LlmUsage`·`ChoicesResult`)에는 같은 가드가 이미 있는데 스키마 층에만
없었다. 여기가 백엔드로 나가는 마지막 관문이라 오히려 더 필요하다.
"""

import pytest
from pydantic import ValidationError

from src.schemas.response_meta import ChatResponseMeta, StoryResponseMeta

_STORY_META_WITHOUT_PROVIDER = {
    "model": "deepseek-v4-flash",
    "prompt_versions": {"STORYLINES": 1},
    "input_token_count": 10,
    "output_token_count": 20,
    "retry_count": 0,
}

_CHAT_META_WITHOUT_PROVIDER = {
    "model": "deepseek-v4-flash",
    "prompt_versions": {"CORE": 1},
    "input_token_count": 10,
    "output_token_count": 20,
    "retry_count": 0,
}


def test_story_meta_requires_an_explicit_provider() -> None:
    """storylines·compile·choices가 공유하는 REST 메타 — provider에 기본값이 없어야 한다."""
    with pytest.raises(ValidationError):
        StoryResponseMeta(**_STORY_META_WITHOUT_PROVIDER)


def test_chat_meta_requires_an_explicit_provider() -> None:
    """chat completed 이벤트 메타 — 같은 이유로 기본값을 두지 않는다."""
    with pytest.raises(ValidationError):
        ChatResponseMeta(**_CHAT_META_WITHOUT_PROVIDER)
