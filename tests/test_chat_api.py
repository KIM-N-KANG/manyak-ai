import json

import pytest

from src.api.v1 import chat as chat_module


def _payload() -> dict:
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
    }


@pytest.fixture
def mock_events(monkeypatch):
    """엔드포인트가 쓰는 stream_chat_turn을 가짜 이벤트 시퀀스로 바꾼다(LLM 회피)."""

    def _set(events: list[dict]) -> None:
        async def _fake(messages):
            for e in events:
                yield e

        monkeypatch.setattr(chat_module, "stream_chat_turn", lambda m: _fake(m))

    return _set


def _data_of(body: str, event: str) -> dict:
    """SSE 본문에서 특정 event의 data(JSON)를 파싱한다."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line == f"event: {event}":
            return json.loads(lines[i + 1][len("data: "):])
    raise AssertionError(f"event {event} 없음:\n{body}")


async def test_chat_turn_sse_token_and_completed(client, mock_events) -> None:
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "choices": ["가", "나", "다"]},
        ]
    )
    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: token" in body
    assert "event: completed" in body

    # completed는 와이어 계약 키 aiOutput으로 직렬화된다(snake ai_output 아님)
    assert "aiOutput" in body
    assert "ai_output" not in body

    completed = _data_of(body, "completed")
    assert completed == {"aiOutput": "안녕", "choices": ["가", "나", "다"]}
    assert _data_of(body, "token") == {"text": "안녕"}


async def test_chat_turn_sse_error(client, mock_events) -> None:
    mock_events([{"event": "error", "code": "LLM_ERROR", "message": "실패"}])
    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    assert _data_of(resp.text, "error") == {"code": "LLM_ERROR", "message": "실패"}
