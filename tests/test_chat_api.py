import json

import pytest

from src.api.v1 import chat as chat_module
from src.services.chat_next_actions import ChoicesResult


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
    """엔드포인트가 쓰는 stream_chat_turn을 가짜 이벤트 시퀀스로 바꾼다(본문 LLM 회피)."""

    def _set(events: list[dict]) -> None:
        async def _fake(messages):
            for e in events:
                yield e

        monkeypatch.setattr(chat_module, "stream_chat_turn", lambda m: _fake(m))

    return _set


@pytest.fixture
def mock_next_actions(monkeypatch):
    """선택지 호출(generate_choices)을 고정 결과로 바꾼다(선택지 LLM 회피)."""

    def _set(result: ChoicesResult) -> None:
        async def _fake(req, ai_output):
            return result

        monkeypatch.setattr(chat_module, "generate_choices", _fake)

    return _set


def _data_of(body: str, event: str) -> dict:
    """SSE 본문에서 특정 event의 data(JSON)를 파싱한다."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line == f"event: {event}":
            return json.loads(lines[i + 1][len("data: "):])
    raise AssertionError(f"event {event} 없음:\n{body}")


async def test_chat_turn_sse_token_and_completed(client, mock_events, mock_next_actions) -> None:
    # 본문 스트림은 choices를 싣지 않는다. 엔드포인트가 선택지 호출 결과를 합쳐 completed로 낸다.
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash"},
        ]
    )
    mock_next_actions(
        ChoicesResult(
            choices=["가", "나", "다"],
            input_tokens=None,
            output_tokens=None,
            retry_count=0,
            model="deepseek-v4-flash",
        )
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
    assert completed["aiOutput"] == "안녕"
    assert completed["choices"] == ["가", "나", "다"]  # 선택지 호출 결과가 합쳐진다
    assert _data_of(body, "token") == {"text": "안녕"}

    # 로깅 메타(KNK-243): chat은 camelCase 와이어
    meta = completed["meta"]
    assert meta["provider"] == "deepseek"
    assert meta["retryCount"] == 0
    assert meta["promptVersions"]["SAFETY"] >= 1  # 6레이어 버전 객체
    assert meta["promptVersions"]["NEXT_ACTIONS"] >= 1  # 선택지 프롬프트 버전 합류
    assert meta["model"]  # 이벤트에 model 없으면 설정값으로 폴백
    # mock 이벤트·선택지 모두 토큰이 없어 null
    assert meta["inputTokenCount"] is None
    assert meta["outputTokenCount"] is None
    assert "input_token_count" not in body  # snake_case 아님(chat은 camel)


async def test_chat_turn_merges_tokens_and_retry(client, mock_events, mock_next_actions) -> None:
    # 본문·선택지 토큰은 합산되고, retryCount는 선택지 재호출 횟수가 실린다.
    mock_events(
        [{"event": "completed", "ai_output": "장면", "model": "deepseek-v4-flash",
          "input_tokens": 100, "output_tokens": 40}]
    )
    mock_next_actions(
        ChoicesResult(
            choices=["가", "나", "다"], input_tokens=30, output_tokens=12,
            retry_count=2, model="deepseek-v4-flash",
        )
    )
    resp = await client.post("/api/v1/chat/turns", json=_payload())
    meta = _data_of(resp.text, "completed")["meta"]
    assert meta["inputTokenCount"] == 130  # 100 + 30
    assert meta["outputTokenCount"] == 52  # 40 + 12
    assert meta["retryCount"] == 2


async def test_chat_turn_sse_error(client, mock_events) -> None:
    # 본문이 error로 끝나면 그 error만 relay하고 선택지 호출은 하지 않는다.
    mock_events([{"event": "error", "code": "LLM_ERROR", "message": "실패"}])
    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    assert _data_of(resp.text, "error") == {"code": "LLM_ERROR", "message": "실패"}
