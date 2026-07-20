"""선택지 전용 엔드포인트(/chat/choices) API 테스트 — KNK-626.

/chat/turns에서 분리된 동기 REST 호출의 계약을 검증한다: 요청은 턴 재료 + ai_output,
응답은 choices 3개 + snake_case meta(항상 200 — 실패는 generate_choices가 흡수).
LLM 호출은 monkeypatch로 회피한다(생성 로직 자체는 tests/unit/test_chat_choices.py 소유).
"""

import pytest

from src.api.v1 import chat as chat_module
from src.services.chat_choices import ChoicesResult


def _payload() -> dict:
    """유효한 선택지 요청 본문 — 턴 요청 재료 + 방금 생성된 본문(ai_output)."""
    return {
        "genre": "판타지",
        "story_settings": {
            "world_setting": "# 세계관\n아르덴 왕국.",
            "character_setting": "# 등장인물\n## 레이\n냉정하다.",
            "user_role_setting": "# 주인공\n카이",
            "rule_setting": "# 전개 규칙\n빌드업 후.",
        },
        "start_settings": {
            "name": "장례식 밤",
            "prologue": "깊은 밤.",
            "start_situation": "레이가 들어선다.",
        },
        "history": [{"role": "ASSISTANT", "content": "*오프닝*"}],
        "user_input": "용건이 뭐요?",
        "summary": "",
        "ai_output": "*레이가 천천히 고개를 든다* 부탁이 있소.",
    }


@pytest.fixture
def mock_choices(monkeypatch):
    """선택지 호출(generate_choices)을 고정 결과로 바꾸고, 받은 인자를 기록한다."""
    calls: list[tuple] = []

    def _set(result: ChoicesResult) -> list[tuple]:
        async def _fake(req, ai_output):
            calls.append((req, ai_output))
            return result

        monkeypatch.setattr(chat_module, "generate_choices", _fake)
        return calls

    return _set


async def test_chat_choices_returns_three_with_snake_meta(client, mock_choices) -> None:
    # 정상 경로: 200 + choices 3개 + snake_case meta(NEXT_ACTIONS 버전·토큰·재호출 횟수).
    calls = mock_choices(
        ChoicesResult(
            choices=["가", "나", "다"],
            input_tokens=30,
            output_tokens=12,
            retry_count=1,
            model="deepseek-v4-flash",
        )
    )
    resp = await client.post("/api/v1/chat/choices", json=_payload())

    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"] == ["가", "나", "다"]

    # meta는 story 계열과 같은 snake_case다(camelCase는 chat SSE만의 예외)
    meta = data["meta"]
    assert meta["provider"] == "deepseek"
    assert meta["retry_count"] == 1
    assert meta["input_token_count"] == 30
    assert meta["output_token_count"] == 12
    assert meta["prompt_versions"]["NEXT_ACTIONS"] >= 1
    assert meta["model"] == "deepseek-v4-flash"
    assert "retryCount" not in resp.text  # camelCase 누출 없음

    # 서비스에는 요청 객체(턴 재료)와 ai_output이 분리되어 전달된다
    (req, ai_output), = calls
    assert ai_output == "*레이가 천천히 고개를 든다* 부탁이 있소."
    assert req.user_input == "용건이 뭐요?"


async def test_chat_choices_fallback_result_is_still_200(client, mock_choices) -> None:
    # 폴백까지 간 결과(재호출 2회 소진·토큰 없음)도 그대로 200으로 나간다 —
    # '항상 200 + 정확히 3개' 계약(실패 흡수는 generate_choices 소유).
    mock_choices(
        ChoicesResult(
            choices=["*잠시 멈춰 주변을 살핀다*", "*한 걸음 물러선다*", "*침묵한다*"],
            input_tokens=None,
            output_tokens=None,
            retry_count=2,
            model="deepseek-v4-flash",
        )
    )
    resp = await client.post("/api/v1/chat/choices", json=_payload())

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["choices"]) == 3
    assert data["meta"]["retry_count"] == 2
    assert data["meta"]["input_token_count"] is None


async def test_chat_choices_requires_ai_output(client, mock_choices) -> None:
    # ai_output 없는 요청은 422 — 턴 요청과 구분되는 이 계약의 필수 추가 필드.
    mock_choices(
        ChoicesResult(choices=["가", "나", "다"], input_tokens=None,
                      output_tokens=None, retry_count=0, model="m")
    )
    payload = _payload()
    del payload["ai_output"]
    resp = await client.post("/api/v1/chat/choices", json=payload)

    assert resp.status_code == 422
