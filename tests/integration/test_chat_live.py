import json
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


def _sse_data(body: str, event: str) -> dict:
    """SSE 응답 본문에서 지정 event 프레임의 data(JSON)를 뽑는다."""
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        lines = frame.splitlines()
        if lines and lines[0] == f"event: {event}":
            data_line = next(ln for ln in lines if ln.startswith("data: "))
            return json.loads(data_line[len("data: "):])
    raise AssertionError(f"{event} 프레임이 응답에 없습니다: {body!r}")


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
    req = _request()
    events = [e async for e in stream_chat_turn(assemble(req))]

    types = [e["event"] for e in events]
    assert "error" not in types, [e for e in events if e["event"] == "error"]
    assert "completed" in types

    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")

    # 본문을 실제로 흘렸고 비어 있지 않다
    assert tokens.strip()
    assert completed["ai_output"].strip()
    # 본문 호출은 선택지를 만들지 않는다 — 마커도, choices 키도 섞이지 않는다(별도 호출 전담).
    assert "[다음 행동]" not in tokens
    assert "[다음 행동]" not in completed["ai_output"]
    assert "choices" not in completed

    # 로깅 메타(KNK-243): include_usage로 실 DeepSeek이 토큰을 채우는지는 라이브만 관측한다.
    # 공급자가 usage 방식을 바꾸면 백엔드 ai_call_logs 토큰이 조용히 null이 되는 회귀를 잡는다.
    assert completed["input_tokens"] and completed["input_tokens"] > 0
    assert completed["output_tokens"] and completed["output_tokens"] > 0
    assert completed["model"]
    # 선택지 생성기 직접 호출은 두지 않는다 — 생성기 분기는 유닛(test_chat_choices.py)이
    # 전부 덮고, 실 LLM 검증은 전용 엔드포인트 테스트(아래 full_path)가 담당한다(중복 과금 제거).


async def test_chat_turn_full_path_live(client) -> None:
    """전 구간 결합(HTTP → 조립 → 실 LLM → SSE 직렬화)을 ASGI client로 최소 1개 확보한다.

    다른 라이브 테스트가 서비스 함수를 직접 부르는 것과 달리, 여기서는 실제 엔드포인트를
    거쳐 '실 LLM × 와이어 계약'을 검증한다(aiOutput camelCase·빈 choices·본문 meta 토큰).
    선택지 분리(KNK-625) 후 completed는 선택지를 기다리지 않는다 — 전용 엔드포인트의
    전 구간 검증은 test_chat_choices_full_path_live가 담당한다.
    """
    payload = _request().model_dump(mode="json")
    resp = await client.post("/api/v1/chat/turns", json=payload)

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: error" not in body, body
    assert "event: completed" in body

    completed = _sse_data(body, "completed")
    assert completed["aiOutput"].strip()  # 와이어 계약 키는 camelCase
    assert completed["choices"] == []  # 선택지는 /chat/choices로 분리 — 하위호환 빈 배열
    # meta 토큰(본문, 재료 없어 판정 스킵)이 실 usage로 채워진다.
    assert completed["meta"]["inputTokenCount"] and completed["meta"]["inputTokenCount"] > 0
    assert completed["meta"]["retryCount"] == 0  # 본문·판정은 재호출 없음


async def test_chat_choices_full_path_live(client) -> None:
    """선택지 전용 엔드포인트(/chat/choices)의 전 구간 결합(HTTP → 실 LLM → 계약) 검증.

    턴 재료 + 방금 본문(ai_output)을 실어 호출하면 실 LLM으로 정확히 3개가 오고,
    snake_case meta(토큰·재호출 횟수)가 실 usage로 채워지는지 확인한다(KNK-625).
    """
    payload = _request().model_dump(mode="json")
    payload["ai_output"] = (
        "*레이가 촛불 앞에 멈춰 서서 당신을 바라본다.*\n\n"
        "레이: 선왕의 침소에서 마지막 밤에 무엇을 보셨는지, 이제는 말씀해 주셔야겠습니다."
    )
    resp = await client.post("/api/v1/chat/choices", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["choices"]) == 3
    assert all(c.strip() for c in data["choices"])
    # meta(snake_case 계약)가 실 usage로 채워진다 — 백엔드 choice_generation 행 적재 재료.
    meta = data["meta"]
    assert meta["input_token_count"] and meta["input_token_count"] > 0
    assert meta["output_token_count"] and meta["output_token_count"] > 0
    assert meta["model"]
    assert meta["provider"] == "deepseek"
    assert 0 <= meta["retry_count"] <= 2  # 누적 재호출 상한(_MAX_REFILL)
    assert meta["prompt_versions"]["NEXT_ACTIONS"] >= 1
