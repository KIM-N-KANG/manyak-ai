import os

import pytest

from src.schemas.chat_turn import (
    ChatHistoryItem,
    ChatStartSettings,
    ChatStorySettings,
    ChatTurnRequest,
)
from src.services.chat_assembler import assemble
from src.services.chat_llm import stream_chat_turn


@pytest.fixture(autouse=True)
def require_live_env() -> None:
    # 실제 LLM을 호출하므로 의도적으로 켤 때만 실행한다(CI 더미 키로는 스킵).
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("RUN_LIVE_TESTS=1이 아니면 라이브 통합 테스트를 건너뜁니다")


def _request() -> ChatTurnRequest:
    return ChatTurnRequest(
        genre="판타지",
        story_settings=ChatStorySettings(
            world_setting="# 세계관\n아르덴 왕국은 마법이 쇠퇴한 시대다. 귀족들이 왕위를 두고 다툰다.\n\n# 갈등\n왕위 계승 분쟁이 언제든 내전으로 번질 수 있다.",
            character_setting="# 등장인물\n\n## 레이\n### 성격\n냉정하고 계산적이다.\n### 말투\n격식 있는 존댓말.\n### 주인공을 대하는 태도\n경계하며 이용 가치를 가늠한다.",
            user_role_setting="# 주인공\n## 호칭\n카이\n## 역할\n왕실 근위 기사\n## 성격\n우직하고 의리 있다.",
            rule_setting="# 전개 규칙\n결정적 사건은 충분한 빌드업 뒤에만.\n\n# 문체 톤\n긴장감 있는 서술.\n\n# 분량 배분\n묘사 5 : 대사 5",
        ),
        start_settings=ChatStartSettings(
            name="장례식 밤의 방문",
            prologue="선왕의 장례가 끝난 깊은 밤. 카이는 빈소를 지키는 마지막 근위 기사로 남았다.",
            start_situation="촛불만 흔들리는 빈소에 레이가 소리 없이 들어선다.",
        ),
        history=[
            ChatHistoryItem(
                role="ASSISTANT",
                content="*레이가 빈소로 들어선다.*\n\n레이: 늦은 밤 실례합니다, 기사님.",
            )
        ],
        user_input="검 손잡이에 손을 올린 채 묻는다. 무슨 용건이오?",
        summary="",
    )


async def test_chat_turn_live() -> None:
    events = [e async for e in stream_chat_turn(assemble(_request()))]

    types = [e["event"] for e in events]
    assert "error" not in types, [e for e in events if e["event"] == "error"]
    assert "completed" in types

    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")

    # 본문을 실제로 흘렸고 비어 있지 않다
    assert tokens.strip()
    assert completed["ai_output"].strip()
    # B안: 선택지 마커는 token으로 흘리지 않는다
    assert "[다음 행동]" not in tokens
    # 본문(ai_output)에도 선택지 마커가 섞이지 않는다
    assert "[다음 행동]" not in completed["ai_output"]
    # LLM이 CORE 출력 봉투(다음 행동 3개)를 지켜 choices가 파싱된다
    assert len(completed["choices"]) == 3
    assert all(c.strip() for c in completed["choices"])
