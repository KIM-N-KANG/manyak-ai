import asyncio
import json

import pytest

from src.api.v1 import chat as chat_module
from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE,
    EVENT_PING,
    ChatTurnRequest,
    PingData,
    TargetMainEventOut,
)
from src.services import chat_judgement as chat_judgement_module
from src.services.chat_judgement import JudgementResult


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
        async def _fake(messages, *, character_images):
            for e in events:
                yield e

        monkeypatch.setattr(
            chat_module,
            "stream_chat_turn",
            lambda messages, *, character_images: _fake(
                messages, character_images=character_images
            ),
        )

    return _set


@pytest.fixture
def mock_judgement(monkeypatch):
    """판정 호출(generate_judgement)을 고정 결과로 바꾼다(판정 LLM 회피)."""

    def _set(result: JudgementResult) -> None:
        async def _fake(req, ai_output, budget_seconds=None):
            return result

        monkeypatch.setattr(chat_module, "generate_judgement", _fake)

    return _set


def _data_of(body: str, event: str) -> dict:
    """SSE 본문에서 특정 event의 data(JSON)를 파싱한다."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line == f"event: {event}":
            return json.loads(lines[i + 1][len("data: "):])
    raise AssertionError(f"event {event} 없음:\n{body}")


async def test_chat_turn_sse_token_and_completed(client, mock_events) -> None:
    # 선택지 분리(KNK-625) 후: completed는 선택지 호출 없이 본문·판정만으로 발행되고,
    # choices는 하위호환 빈 배열 고정이다(백엔드는 '빈 배열이면 저장하지 않음').
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            # provider는 일부러 "deepseek"이 아닌 값을 넣는다 — 둘 다 deepseek이면
            # "제대로 옮긴 값"과 "코드에 박아둔 상수"를 구분할 수 없다(KNK-674 리뷰 H1).
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "not-deepseek"},
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
    assert completed["aiOutput"] == "안녕"
    assert completed["choices"] == []  # 선택지는 /chat/choices로 분리 — 빈 배열 고정
    assert _data_of(body, "token") == {"text": "안녕"}

    # 로깅 메타(KNK-243): chat은 camelCase 와이어
    meta = completed["meta"]
    assert meta["provider"] == "not-deepseek"  # 이벤트에 실린 값이 그대로 meta로
    assert meta["retryCount"] == 0  # 본문·판정은 재호출 없음 — 0 고정
    assert meta["promptVersions"]["SAFETY"] >= 1  # 6레이어 버전 객체
    assert "NEXT_ACTIONS" not in meta["promptVersions"]  # 선택지 버전 키는 /chat/choices로 이동
    assert meta["model"]  # 이벤트에 model 없으면 설정값으로 폴백
    # mock 이벤트에 토큰이 없어 null
    assert meta["inputTokenCount"] is None
    assert meta["outputTokenCount"] is None
    assert "input_token_count" not in body  # snake_case 아님(chat은 camel)

    # 판정 메타(재료 없는 현행 요청): generate_judgement가 스킵돼 3필드 null,
    # JUDGEMENT 버전 키는 항상 실린다. (generate_judgement는 모킹하지 않아 실제 스킵 경로를 탄다.)
    assert completed["targetMainEvent"] is None
    assert completed["occurredMainEventName"] is None
    assert completed["endingName"] is None
    assert meta["promptVersions"]["JUDGEMENT"] >= 1


async def test_chat_turn_serializes_character_image_event(client, mock_events) -> None:
    mock_events(
        [
            {
                "event": EVENT_CHARACTER_IMAGE,
                "name": "세린",
                "image_url": "https://cdn.example.com/serin.webp",
            },
            {
                "event": "completed",
                "ai_output": (
                    "[[세린:https://cdn.example.com/serin.webp]]세린: 안녕."
                ),
                "character_images": [
                    {
                        "name": "세린",
                        "image_url": "https://cdn.example.com/serin.webp",
                    }
                ],
                "model": "deepseek-v4-flash",
                "provider": "deepseek",
            },
        ]
    )
    payload = _payload()
    payload["character_images"] = [
        {"name": "세린", "image_url": "https://cdn.example.com/serin.webp"}
    ]

    response = await client.post("/api/v1/chat/turns", json=payload)

    assert response.status_code == 200
    assert _data_of(response.text, EVENT_CHARACTER_IMAGE) == {
        "name": "세린",
        "imageUrl": "https://cdn.example.com/serin.webp",
    }
    completed = _data_of(response.text, "completed")
    assert completed["aiOutput"] == (
        "[[세린:https://cdn.example.com/serin.webp]]세린: 안녕."
    )
    assert completed["characterImages"] == [
        {
            "name": "세린",
            "imageUrl": "https://cdn.example.com/serin.webp",
        }
    ]


async def test_chat_turn_meta_body_tokens_and_fixed_retry(client, mock_events) -> None:
    # 선택지 분리 후 토큰 합산은 본문(+판정)만이고, retryCount는 0 고정이다 —
    # 선택지 몫(토큰·재호출 횟수)이 completed meta에 섞이지 않는지 고정하는 회귀 그물.
    mock_events(
        [{"event": "completed", "ai_output": "장면", "model": "deepseek-v4-flash",
          "provider": "deepseek", "input_tokens": 100, "output_tokens": 40}]
    )
    resp = await client.post("/api/v1/chat/turns", json=_payload())
    meta = _data_of(resp.text, "completed")["meta"]
    assert meta["inputTokenCount"] == 100  # 본문만(판정은 재료 없어 스킵)
    assert meta["outputTokenCount"] == 40
    assert meta["retryCount"] == 0


async def test_chat_turn_sse_error(client, mock_events) -> None:
    # 본문이 error로 끝나면 그 error만 relay하고 선택지 호출은 하지 않는다.
    mock_events([{"event": "error", "code": "LLM_ERROR", "message": "실패"}])
    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    assert _data_of(resp.text, "error") == {"code": "LLM_ERROR", "message": "실패"}


async def test_chat_turn_completed_serializes_judgement_meta(
    client, mock_events, mock_judgement
) -> None:
    # 재료가 실린 턴: 판정 3필드가 completed까지 camelCase로 직렬화되고, meta.promptVersions에
    # JUDGEMENT 키가 실리며, 판정 토큰이 합산되는지 엔드포인트 전체 흐름으로 확인한다(#3 리뷰 반영).
    mock_events([{"event": "completed", "ai_output": "장면", "model": "deepseek-v4-flash",
                  "provider": "deepseek"}])
    mock_judgement(
        JudgementResult(
            target_main_event=TargetMainEventOut(name="반란의 증거", progress_turns=2),
            occurred_main_event_name="선왕의 유언",
            ending_name="왕좌를 되찾다",
            input_tokens=15,
            output_tokens=5,
        )
    )
    payload = {
        **_payload(),
        "main_events": [
            {"name": "반란의 증거", "description": "밀서.", "key_sentence": "밀서를 손에 넣는다."},
            {"name": "선왕의 유언", "description": "유언장.", "key_sentence": "유언장을 찾는다."},
        ],
        "target_main_event": {"name": "반란의 증거", "progress_turns": 1},
        "occurred_main_event_names": [],
        "endings": [
            {"name": "왕좌를 되찾다", "achievement_condition": "둘 다 확보.", "epilogue": "대관식."}
        ],
    }
    resp = await client.post("/api/v1/chat/turns", json=payload)

    assert resp.status_code == 200
    body = resp.text
    completed = _data_of(body, "completed")

    # 판정 3필드가 camelCase 와이어 키로 직렬화된다
    assert completed["targetMainEvent"] == {"name": "반란의 증거", "progressTurns": 2}
    assert completed["occurredMainEventName"] == "선왕의 유언"
    assert completed["endingName"] == "왕좌를 되찾다"
    assert "progress_turns" not in body  # snake_case 누출 없음

    # meta.promptVersions에 JUDGEMENT 키가 실리고, 판정 토큰이 합산된다(본문 토큰 없음 → 15/5)
    meta = completed["meta"]
    assert meta["promptVersions"]["JUDGEMENT"] >= 1
    assert meta["inputTokenCount"] == 15
    assert meta["outputTokenCount"] == 5


async def test_chat_turn_trace_receives_connection_metadata(
    client, mock_events, monkeypatch
) -> None:
    """채팅 턴 트레이스는 호출별 연결값만 받고 장르 태그는 받지 않는다.

    dimension_tags 시그니처 테스트는 헬퍼 경유 부활만 막는다 — 인라인 태그(tags=[...])로
    되돌려도 잡히도록, 엔드포인트가 observe_request에 tags 인자 자체를 넘기지 않음을 본다
    (장르 태그는 스토리 제작 트레이스에만 — 5-ai-server §5-6).
    """
    from contextlib import contextmanager

    captured: dict = {}

    @contextmanager
    def _fake_observe(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)

        class _Trace:
            def set_metadata(self, **kw) -> None: ...

        yield _Trace()

    monkeypatch.setattr(chat_module, "observe_request", _fake_observe)
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )
    payload = {**_payload(), "user_source": "edited_choice"}
    resp = await client.post(
        "/api/v1/chat/turns",
        json=payload,
        headers={
            "X-Manyak-Creation-Id": "11111111-1111-1111-1111-111111111111",
            "X-Manyak-Story-Id": "22222222-2222-2222-2222-222222222222",
            "X-Manyak-Chat-Id": "33333333-3333-3333-3333-333333333333",
            "X-Manyak-Start-Setting-Id": "44444444-4444-4444-4444-444444444444",
            "X-Manyak-Turn-Number": "7",
            "X-Manyak-Is-Regenerated": "true",
            "X-Manyak-Storyline-Id": "42",
        },
    )

    assert resp.status_code == 200
    assert captured["name"] == "채팅 턴"
    assert captured["input_data"] == ChatTurnRequest.model_validate(payload).model_dump(
        mode="json"
    )
    assert captured["metadata"] == {
        "creation_id": "11111111-1111-1111-1111-111111111111",
        "story_id": "22222222-2222-2222-2222-222222222222",
        "chat_id": "33333333-3333-3333-3333-333333333333",
        "start_setting_id": "44444444-4444-4444-4444-444444444444",
        "turn_number": 7,
        "is_regenerated": True,
        "user_source": "edited_choice",
        "prompt_versions": {
            **chat_module.LAYER_VERSIONS,
            "JUDGEMENT": chat_module.JUDGEMENT_VERSION,
        },
        "retry_count": 0,
    }
    assert "tags" not in captured  # 어떤 경로로든 태그가 실리면 실패


async def test_chat_turn_ignores_unknown_user_source(client, mock_events, caplog) -> None:
    """관측용 입력 출처가 새 값이어도 채팅은 422로 거부하지 않는다."""
    raw_user_source = "random_new_value_from_backend"
    mock_events([{"event": "error", "code": "LLM_ERROR", "message": "실패"}])

    response = await client.post(
        "/api/v1/chat/turns",
        json={**_payload(), "user_source": raw_user_source},
    )

    assert response.status_code == 200
    assert "Langfuse user_source 값 무시" in caplog.text
    assert raw_user_source not in caplog.text


async def test_concurrent_chat_turn_streams_keep_connection_metadata_isolated(
    client, monkeypatch
) -> None:
    """동시에 흐르는 SSE 두 건이 각 요청의 연결값을 스트림 안까지 따로 가져간다."""
    from contextlib import contextmanager

    captured: list[tuple[str, dict[str, object]]] = []
    both_streams_started = asyncio.Event()
    started = 0

    @contextmanager
    def _fake_observe(_name, **kwargs):
        captured.append((kwargs["input_data"]["user_input"], kwargs["metadata"]))

        class _Trace:
            def set_metadata(self, **kw) -> None: ...

        yield _Trace()

    async def _events(_messages):
        nonlocal started
        started += 1
        if started == 2:
            both_streams_started.set()
        await asyncio.wait_for(both_streams_started.wait(), timeout=5)
        yield {
            "event": "completed",
            "ai_output": "응답",
            "model": "deepseek-v4-flash",
            "provider": "deepseek",
        }

    monkeypatch.setattr(chat_module, "observe_request", _fake_observe)
    monkeypatch.setattr(
        chat_module,
        "stream_chat_turn",
        lambda messages, *, character_images: _events(messages),
    )

    async def _post(label: str, chat_id: str, turn_number: int):
        return await client.post(
            "/api/v1/chat/turns",
            json={**_payload(), "user_input": label, "user_source": "typed"},
            headers={
                "X-Manyak-Chat-Id": chat_id,
                "X-Manyak-Turn-Number": str(turn_number),
                "X-Manyak-Is-Regenerated": "false",
            },
        )

    responses = await asyncio.gather(
        _post("요청 A", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 1),
        _post("요청 B", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 2),
    )

    assert all(response.status_code == 200 for response in responses)
    by_input = {user_input: metadata for user_input, metadata in captured}
    assert {
        key: by_input["요청 A"][key]
        for key in ("chat_id", "turn_number", "is_regenerated", "user_source")
    } == {
        "chat_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "turn_number": 1,
        "is_regenerated": False,
        "user_source": "typed",
    }
    assert {
        key: by_input["요청 B"][key]
        for key in ("chat_id", "turn_number", "is_regenerated", "user_source")
    } == {
        "chat_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "turn_number": 2,
        "is_regenerated": False,
        "user_source": "typed",
    }


async def test_chat_turn_survives_completed_without_provider(
    client, mock_events, other_provider_model
) -> None:
    """완료 이벤트에 provider가 없어도 턴이 조용히 사라지지 않는다(KNK-674 리뷰 M2).

    이 자리에서 예외가 나면 이미 200으로 열린 SSE라 상태를 못 바꾸고, error 이벤트도
    completed도 없이 끊긴다 — 사용자 화면엔 글이 떴는데 백엔드는 그 턴을 저장하지 못한다.
    지금 키가 빠지는 경로는 없지만, 빠졌을 때의 모양이 나빠 폴백을 둔다.

    **DeepSeek이 아닌 모델로 확인한다.** 정답이 "deepseek"이면 폴백이 등록부를 조회한
    것인지 그냥 상수를 적어둔 것인지 구분되지 않는다(KNK-674 2차 리뷰에서 상수 변이가
    살아남았다).
    """
    other_provider_model(chat_module)
    mock_events([{"event": "completed", "ai_output": "장면", "model": "not-deepseek-model"}])

    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    meta = _data_of(resp.text, "completed")["meta"]  # completed가 실제로 나온다
    assert meta["provider"] == "not-deepseek"  # 설정 모델을 등록부로 풀어 채운다


async def test_chat_turn_does_not_paper_over_an_empty_provider(client, mock_events) -> None:
    """빈 provider는 폴백으로 덮지 않는다 — 고장 신호가 기록에서 사라지면 안 된다.

    폴백을 `ev.get("provider") or ...`로 되돌리면 빈 문자열까지 폴백을 타서, 아래 단언이
    "deepseek"을 받아 깨진다(KNK-674 2차 리뷰).
    """
    mock_events(
        [
            {
                "event": "completed",
                "ai_output": "장면",
                "model": "deepseek-v4-flash",
                "provider": "",
            }
        ]
    )

    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    meta = _data_of(resp.text, "completed")["meta"]
    assert meta["provider"] == ""  # 그럴듯한 값으로 메꾸지 않는다


# ── 판정 대기 중 ping (KNK-750 회귀) ─────────────────────────────────────────
# 판정을 그냥 await하면 그 구간에 SSE 프레임이 하나도 안 나가고, 백엔드의 이벤트 간
# 상한(60초)이 그 침묵을 세다가 정상 턴을 끊는다(KNK-748 — 3주간 3건). 기다리는 동안
# ping이 실제로 흘러나오는지, completed보다 먼저 나오는지, data 줄을 갖췄는지 고정한다.
# (data 줄이 없으면 백엔드 디코더가 항목으로 만들지 않아 시계가 안 돌아간다.)
async def test_chat_turn_pings_while_judgement_is_slow(
    client, mock_events, monkeypatch
) -> None:
    monkeypatch.setattr(chat_module, "_JUDGEMENT_PING_INTERVAL_SECONDS", 0.02)

    async def _slow(req, ai_output, budget_seconds=None):
        await asyncio.sleep(0.5)  # 간격의 25배 — 루프가 잠깐 밀려도 ping이 반드시 나간다
        return JudgementResult(None, None, None, None, None)

    monkeypatch.setattr(chat_module, "generate_judgement", _slow)
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )

    body = (await client.post("/api/v1/chat/turns", json=_payload())).text

    assert body.count("event: ping") >= 1, f"판정 대기 중 ping이 안 나갔다:\n{body}"
    assert body.index("event: ping") < body.index("event: completed"), body
    lines = body.splitlines()
    assert lines[lines.index("event: ping") + 1].startswith("data: "), (
        "ping에 data 줄이 없으면 백엔드가 항목으로 만들지 않아 시계가 안 돌아간다"
    )
    # 신호를 끼워 넣어도 본문 확정은 그대로다.
    assert _data_of(body, "completed")["aiOutput"] == "안녕"


# 평소(판정이 1초 안팎)에는 프레임이 늘어나면 안 된다 — 백엔드·프론트가 받는 스트림 모양이
# 바뀌지 않게 한다. 간격(10초)이 판정보다 훨씬 길어서 한 번도 안 나가는 것이 정상이다.
async def test_chat_turn_does_not_ping_when_judgement_is_fast(
    client, mock_events, mock_judgement
) -> None:
    mock_judgement(JudgementResult(None, None, None, None, None))
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )

    body = (await client.post("/api/v1/chat/turns", json=_payload())).text

    assert "event: ping" not in body, f"판정이 즉시 끝났는데 ping이 나갔다:\n{body}"


# 스트림이 도중에 닫히면(클라이언트 이탈) 판정 호출도 멈춰야 한다. 안 멈추면 아무도 받지
# 않는 호출에 요금만 계속 나간다.
#
# 엔드포인트 대신 제너레이터를 직접 몰아서 닫는다 — 테스트용 ASGI 전송(httpx)은 소비를
# 중간에 멈춰도 앱 쪽 끊김 경로를 타지 않아, 이 자리에서는 실서버(uvicorn)의 동작을
# 재현하지 못한다. 확인 대상은 "닫히면 취소한다"는 우리 쪽 처리다.
async def test_closing_the_stream_cancels_the_judgement_call(
    mock_events, monkeypatch
) -> None:
    monkeypatch.setattr(chat_module, "_JUDGEMENT_PING_INTERVAL_SECONDS", 0.01)
    state = {"cancelled": False}

    async def _hangs(req, ai_output, budget_seconds=None):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return JudgementResult(None, None, None, None, None)

    monkeypatch.setattr(chat_module, "generate_judgement", _hangs)
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )

    stream = chat_module._event_stream(ChatTurnRequest(**_payload()), {})
    async for frame in stream:
        if f"event: {EVENT_PING}" in frame:
            break  # 판정이 아직 도는 중이다
    await stream.aclose()  # 클라이언트가 끊겼을 때 서버가 하는 일

    for _ in range(50):  # 취소가 전파될 틈을 준다
        if state["cancelled"]:
            break
        await asyncio.sleep(0.01)
    assert state["cancelled"], "스트림이 닫혔는데 판정 호출이 계속 돌고 있다"


# ── 판정 예산은 이 턴에 남은 시간이다 (KNK-750 회귀) ──────────────────────────
# 본문이 오래 걸린 턴에 판정 60초를 통째로 주면 둘을 합쳐 백엔드의 전체 상한(120초)을 넘겨
# 턴이 죽는다. ping이 되돌리는 것은 이벤트 간 상한뿐이라 이 시계는 못 멈춘다.
#
# **본문을 실제로 지연시킨다.** 지연이 없으면 구현에서 경과 시간 빼기를 통째로 지워도 답이
# 같아 테스트가 통과한다 — 이름만 "남은 시간"이고 아무것도 검증하지 않는 테스트가 된다
# (코덱스 적대적 리뷰, 2026-08-01).
_BODY_DELAY_SECONDS = 0.3


async def test_judgement_budget_subtracts_the_time_the_body_took(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(chat_module, "_TURN_BUDGET_SECONDS", 5.0)
    monkeypatch.setattr(chat_module, "_SAFETY_MARGIN_SECONDS", 1.0)
    seen: dict = {}

    async def _slow_body(messages):
        yield {"event": "token", "text": "안녕"}
        await asyncio.sleep(_BODY_DELAY_SECONDS)  # 본문이 이만큼 걸린 셈
        yield {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
               "provider": "deepseek"}

    async def _capture(req, ai_output, budget_seconds=None):
        seen["budget"] = budget_seconds
        return JudgementResult(None, None, None, None, None)

    monkeypatch.setattr(
        chat_module,
        "stream_chat_turn",
        lambda messages, *, character_images: _slow_body(messages),
    )
    monkeypatch.setattr(chat_module, "generate_judgement", _capture)

    await client.post("/api/v1/chat/turns", json=_payload())

    # 전체 5초 - 여유 1초 - 본문 0.3초 ≈ 3.7초.
    # 위 상한(3.9)이 핵심이다 — 본문 시간을 안 빼면 4.0이 나와 여기서 걸린다.
    assert 3.4 < seen["budget"] < 3.9, seen["budget"]


# ── 실제 상수로 도는 예산 (KNK-750) ──────────────────────────────────────────
# 위 테스트는 상수를 작은 값으로 덮어쓰고 계산식만 본다. 실제 값(전체 120초·여유 15초)에서
# 무슨 일이 벌어지는지는 여기서 고정한다. 여유를 크게 잡았을 때 평소 턴이 손해를 보면 안 된다.
async def test_real_constants_still_give_a_fast_turn_the_full_cap(
    client, mock_events, monkeypatch
) -> None:
    seen: dict = {}

    async def _capture(req, ai_output, budget_seconds=None):
        seen["budget"] = budget_seconds
        return JudgementResult(None, None, None, None, None)

    monkeypatch.setattr(chat_module, "generate_judgement", _capture)
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )

    await client.post("/api/v1/chat/turns", json=_payload())

    # 120 - 15 - (거의 0) ≈ 105초. 판정 쪽 상한 60초에서 잘리므로 평소 턴은 손해가 없다.
    assert seen["budget"] > chat_judgement_module._TIMEOUT_SECONDS, (
        f"여유를 키운 탓에 평소 턴의 판정 시간이 줄었다 — {seen['budget']}"
    )


# 여유가 남은 시간을 다 먹으면 판정을 아예 부르지 않는다(음수 예산이 그대로 넘어가지 않는다).
# 이 분기는 판정 쪽 유닛에도 있지만, 엔드포인트가 실제로 그 값을 만들어 넘기는지는 여기서만 본다.
async def test_no_budget_left_still_completes_the_turn(
    client, mock_events, monkeypatch
) -> None:
    monkeypatch.setattr(chat_module, "_TURN_BUDGET_SECONDS", 10.0)
    monkeypatch.setattr(chat_module, "_SAFETY_MARGIN_SECONDS", 15.0)  # 여유가 전체보다 크다
    seen: dict = {}

    async def _capture(req, ai_output, budget_seconds=None):
        seen["budget"] = budget_seconds
        return JudgementResult(None, None, None, None, None)

    monkeypatch.setattr(chat_module, "generate_judgement", _capture)
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )

    body = (await client.post("/api/v1/chat/turns", json=_payload())).text

    assert seen["budget"] < 0, f"남은 시간이 음수여야 하는 상황이다 — {seen['budget']}"
    # 예산이 없어도 본문은 정상으로 확정돼야 한다(판정만 비고 턴은 산다).
    assert _data_of(body, "completed")["aiOutput"] == "안녕"


# 예산이 없어 판정을 건너뛴 턴의 completed가 **진행 중이던 목표를 그대로 싣는지** 와이어에서
# 확인한다. 여기서는 판정을 모킹하지 않는다 — 실제 generate_judgement를 태워야 직렬화 키
# (targetMainEvent.progressTurns)까지 계약대로 나가는지 볼 수 있다. 예산이 0 이하면 LLM은
# 부르지 않으므로 이 테스트도 과금되지 않는다.
async def test_skipped_judgement_sends_the_target_back_on_the_wire(
    client, mock_events, monkeypatch
) -> None:
    monkeypatch.setattr(chat_module, "_TURN_BUDGET_SECONDS", 10.0)
    monkeypatch.setattr(chat_module, "_SAFETY_MARGIN_SECONDS", 15.0)  # 여유가 전체보다 크다
    mock_events(
        [
            {"event": "token", "text": "안녕"},
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
             "provider": "deepseek"},
        ]
    )
    payload = _payload() | {
        "main_events": [
            {
                "name": "선왕의 유언",
                "description": "숨겨진 유언장이 드러난다.",
                "key_sentence": "유언장의 행방을 쫓는다.",
            }
        ],
        "target_main_event": {"name": "선왕의 유언", "progress_turns": 4},
    }

    body = (await client.post("/api/v1/chat/turns", json=payload)).text

    completed = _data_of(body, "completed")
    assert completed["targetMainEvent"] == {"name": "선왕의 유언", "progressTurns": 4}, (
        f"목표가 null로 나가면 백엔드가 사건 진행을 지운다 — {completed['targetMainEvent']}"
    )
    assert completed["occurredMainEventName"] is None
    assert completed["endingName"] is None


# ping 페이로드는 스키마가 만든 것이어야 한다 — 호출부가 손으로 적은 dict가 아니라.
async def test_ping_payload_comes_from_the_schema(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(chat_module, "_JUDGEMENT_PING_INTERVAL_SECONDS", 0.02)

    async def _slow(req, ai_output, budget_seconds=None):
        await asyncio.sleep(0.5)
        return JudgementResult(None, None, None, None, None)

    monkeypatch.setattr(chat_module, "generate_judgement", _slow)

    async def _events(messages):
        yield {"event": "token", "text": "안녕"}
        yield {"event": "completed", "ai_output": "안녕", "model": "deepseek-v4-flash",
               "provider": "deepseek"}

    monkeypatch.setattr(
        chat_module,
        "stream_chat_turn",
        lambda messages, *, character_images: _events(messages),
    )

    body = (await client.post("/api/v1/chat/turns", json=_payload())).text

    assert _data_of(body, "ping") == PingData().model_dump() == {}
