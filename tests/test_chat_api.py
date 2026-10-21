import asyncio
import base64
import json
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI

import pytest

from src.api.v1 import chat as chat_module
from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE,
    ChatTurnRequest,
    PingData,
)
from tests.conftest import FakeStream


def _image_slots() -> list[dict]:
    return [{"key": "chat-images/test/turn-1.webp",
             "upload_url": "https://bucket.s3.amazonaws.com/chat-images/test/turn-1.webp?signature=test",
             "public_url": "https://cdn.manyak.app/chat-images/test/turn-1.webp"}]


@pytest.mark.parametrize("endpoint", ["turns", "choices"])
@pytest.mark.parametrize("invalid", ["missing_key", "too_many", "bad_url", "missing_genre"])
async def test_validation_error_hides_signed_url(client, endpoint, invalid) -> None:
    payload = {**_payload(), "image_slots": _image_slots(), "ai_output": "test"}
    if invalid == "missing_key":
        del payload["image_slots"][0]["key"]
    elif invalid == "too_many":
        payload["image_slots"] *= 2
    elif invalid == "bad_url":
        payload["image_slots"][0]["upload_url"] += "#invalid"
    else:
        del payload["genre"]
    response = await client.post(f"/api/v1/chat/{endpoint}", json=payload)
    assert response.status_code == 422
    assert "signature=test" not in response.text
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"type", "loc", "msg"} for error in errors)
    assert errors[0]["loc"][:2] == ["body", "genre" if invalid == "missing_genre" else "image_slots"]
    assert errors[0]["type"] == {"missing_key": "missing", "too_many": "too_long",
                                 "bad_url": "value_error", "missing_genre": "missing"}[invalid]


async def test_non_chat_validation_keeps_default_error_format(client) -> None:
    response = await client.post("/api/v1/story/storylines", json={})
    assert response.status_code == 422
    assert response.json()["detail"][0]["input"] == {}




@pytest.fixture(autouse=True)
def uploaded_child(monkeypatch):
    from src.services import chat_child_image
    from src.core.config import settings

    monkeypatch.setattr(settings, "image_upload_allowed_hosts", ["bucket.s3.amazonaws.com"])

    async def upload(child, slot):
        from dataclasses import replace
        return child if child.error else replace(child, image_url=slot.public_url)

    monkeypatch.setattr(chat_child_image, "upload_child_image", upload)


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
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-flash",
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
    assert meta["retryCount"] == 0  # 본문은 재호출 없음 — 0 고정
    assert meta["promptVersions"] == {"CHAT": 1}
    assert "NEXT_ACTIONS" not in meta["promptVersions"]  # 선택지 버전 키는 /chat/choices로 이동
    assert meta["model"]  # 이벤트에 model 없으면 설정값으로 폴백
    # mock 이벤트에 토큰이 없어 null
    assert meta["inputTokenCount"] is None
    assert meta["outputTokenCount"] is None
    assert "input_token_count" not in body  # snake_case 아님(chat은 camel)


async def test_chat_turn_passes_protagonist_name_to_body_prompt(client, monkeypatch) -> None:
    captured = []

    async def fake_stream(messages, *, character_images):
        captured.extend(messages)
        yield {"event": "completed", "ai_output": "장면", "model": "test", "provider": "test"}

    monkeypatch.setattr(chat_module, "stream_chat_turn", fake_stream)
    payload = _payload()
    payload["story_settings"]["protagonist_name"] = "민우"
    payload["story_settings"]["user_role_setting"] = "신중한 성격의 사립 탐정."

    response = await client.post("/api/v1/chat/turns", json=payload)

    assert response.status_code == 200
    assert "event: completed" in response.text
    protagonist = captured[0]["content"].split("<protagonist>", 1)[1].split("</protagonist>", 1)[0]
    assert "주인공 이름: 민우" in protagonist
    assert "신중한 성격의 사립 탐정." in protagonist
    assert "카이" not in protagonist


async def test_chat_turn_serializes_character_image_event(client, mock_events) -> None:
    mock_events(
        [
            {
                "event": EVENT_CHARACTER_IMAGE,
                "name": "세린",
                "image_name": "세린_기본",
                "image_url": "https://cdn.example.com/serin.webp",
            },
            {
                "event": "completed",
                "ai_output": (
                    "[[https://cdn.example.com/serin.webp]]\n\n세린: 안녕."
                ),
                "character_images": [
                    {
                        "name": "세린",
                        "image_name": "세린_기본",
                        "image_url": "https://cdn.example.com/serin.webp",
                    }
                ],
                "model": "deepseek-flash",
                "provider": "deepseek",
            },
        ]
    )
    payload = _payload()
    payload["character_images"] = [
        {
            "name": "세린",
            "image_name": "세린_기본",
            "image_url": "https://cdn.example.com/serin.webp",
        }
    ]

    response = await client.post("/api/v1/chat/turns", json=payload)

    # 와이어 키는 camelCase — imageName·imageUrl(KNK-1026).
    assert response.status_code == 200
    assert _data_of(response.text, EVENT_CHARACTER_IMAGE) == {
        "name": "세린",
        "imageName": "세린_기본",
        "imageUrl": "https://cdn.example.com/serin.webp",
    }
    completed = _data_of(response.text, "completed")
    assert completed["aiOutput"] == (
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 안녕."
    )
    assert completed["characterImages"] == [
        {
            "name": "세린",
            "imageName": "세린_기본",
            "imageUrl": "https://cdn.example.com/serin.webp",
        }
    ]


async def test_chat_turn_meta_body_tokens_and_fixed_retry(client, mock_events) -> None:
    # 선택지 분리 후 토큰 합산은 본문(+판정)만이고, retryCount는 0 고정이다 —
    # 선택지 몫(토큰·재호출 횟수)이 completed meta에 섞이지 않는지 고정하는 회귀 그물.
    mock_events(
        [{"event": "completed", "ai_output": "장면", "model": "deepseek-flash",
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


async def test_chat_turn_sends_no_judgement_fields_even_with_materials(
    client, mock_events
) -> None:
    # 판정 제거(KNK-1626) 회귀: 사건·엔딩 재료와 옛 목표·완결 사건 필드가 실려도 200으로 받고, 판정 3필드가 없고,
    # meta 토큰은 본문 호출 값만 싣는다(판정 토큰 합산 없음).
    mock_events([{"event": "completed", "ai_output": "장면", "model": "deepseek-flash",
                  "provider": "deepseek", "input_tokens": 10, "output_tokens": 3}])
    payload = {
        **_payload(),
        "main_events": [
            {"name": "반란의 증거", "description": "밀서.", "key_sentence": "밀서를 손에 넣는다."},
        ],
        "target_main_event": {"name": "반란의 증거", "progress_turns": 1},
        "occurred_main_event_names": [],
        "endings": [
            {"name": "왕좌를 되찾다", "achievement_condition": "둘 다 확보.", "epilogue": "대관식."}
        ],
    }
    resp = await client.post("/api/v1/chat/turns", json=payload)

    assert resp.status_code == 200
    completed = _data_of(resp.text, "completed")
    for key in ("targetMainEvent", "occurredMainEventName", "endingName"):
        assert key not in completed
    meta = completed["meta"]
    assert meta["inputTokenCount"] == 10
    assert meta["outputTokenCount"] == 3


async def test_chat_turn_trace_receives_connection_metadata(
    client, mock_events, monkeypatch
) -> None:
    """채팅 턴 트레이스는 호출별 연결값만 받고 장르 태그는 받지 않는다.

    dimension_tags 시그니처 테스트는 헬퍼 경유 부활만 막는다 — 인라인 태그(tags=[...])로
    되돌려도 잡히도록, 엔드포인트가 observe_request에 tags 인자 자체를 넘기지 않음을 본다
    (장르 태그는 스토리 제작 트레이스에만 — spec/5-ai-server-spec.md §5-6).
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
            {"event": "completed", "ai_output": "안녕", "model": "deepseek-flash",
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
        mode="json", exclude={"image_slots"}
    )
    assert captured["metadata"] == {
        "creation_id": "11111111-1111-1111-1111-111111111111",
        "story_id": "22222222-2222-2222-2222-222222222222",
        "chat_id": "33333333-3333-3333-3333-333333333333",
        "start_setting_id": "44444444-4444-4444-4444-444444444444",
        "turn_number": 7,
        "is_regenerated": True,
        "user_source": "edited_choice",
        "prompt_versions": {"CHAT": 1},
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
            "model": "deepseek-flash",
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
                "model": "deepseek-flash",
                "provider": "",
            }
        ]
    )

    resp = await client.post("/api/v1/chat/turns", json=_payload())

    assert resp.status_code == 200
    meta = _data_of(resp.text, "completed")["meta"]
    assert meta["provider"] == ""  # 그럴듯한 값으로 메꾸지 않는다


# ── 대기 중 ping (KNK-750 회귀) ─────────────────────────────────────────────
# 본문·이미지를 기다리는 동안 SSE 프레임이 하나도 안 나가면 백엔드의 이벤트 간 상한(60초)이
# 그 침묵을 세다가 정상 턴을 끊는다(KNK-748). ping이 completed보다 먼저 나오고, data 줄을
# 갖추고, 스키마가 만든 페이로드인지 고정한다. (data 줄이 없으면 백엔드 디코더가 항목으로
# 만들지 않아 시계가 안 돌아간다.)
async def test_chat_turn_pings_while_waiting_for_the_body(
    client, monkeypatch
) -> None:
    from src.services import chat_child_image

    monkeypatch.setattr(chat_child_image, "_PING_INTERVAL_SECONDS", 0.02)

    async def _slow_body(messages):
        yield {"event": "token", "text": "안녕"}
        await asyncio.sleep(0.5)  # 간격의 25배 — 루프가 잠깐 밀려도 ping이 반드시 나간다
        yield {"event": "completed", "ai_output": "안녕", "model": "deepseek-flash",
               "provider": "deepseek"}

    monkeypatch.setattr(
        chat_module,
        "stream_chat_turn",
        lambda messages, *, character_images: _slow_body(messages),
    )

    body = (await client.post("/api/v1/chat/turns", json=_payload())).text

    assert body.count("event: ping") >= 1, f"대기 중 ping이 안 나갔다:\n{body}"
    assert body.index("event: ping") < body.index("event: completed"), body
    lines = body.splitlines()
    assert lines[lines.index("event: ping") + 1].startswith("data: "), (
        "ping에 data 줄이 없으면 백엔드가 항목으로 만들지 않아 시계가 안 돌아간다"
    )
    assert _data_of(body, "ping") == PingData().model_dump() == {}
    assert _data_of(body, "completed")["aiOutput"] == "안녕"


@pytest.mark.parametrize("legacy_flag", [None, False, True])
async def test_child_image_slot_emits_uploaded_url_without_image_data(client, mock_events, monkeypatch, legacy_flag) -> None:
    from unittest.mock import AsyncMock
    from src.services import chat_child_image
    from src.services.image.generate_child import ChildImageResult

    child = AsyncMock(return_value=ChildImageResult("레이", "레이_실시간_test", "image-data"))
    monkeypatch.setattr(chat_child_image, "generate_child_image", child)
    mock_events([
        {"event": "token", "text": "*문이 열린다.*\n레이: 안녕."},
        {"event": "completed", "ai_output": "*문이 열린다.*\n레이: 안녕."},
    ])
    payload = _payload()
    payload["image_slots"] = _image_slots()
    if legacy_flag is not None:
        payload["generate_child_image"] = legacy_flag
    payload["character_images"] = [{"name": "레이", "image_name": "레이_기본", "image_url": "https://cdn.manyak.app/parent.webp"}]
    response = await client.post("/api/v1/chat/turns", json=payload)
    image = _data_of(response.text, "character_image")
    assert image["imageUrl"] == payload["image_slots"][0]["public_url"]
    assert image["imageName"] == "레이_실시간_test"
    assert "generatedImage" not in response.text and "image-data" not in response.text
    completed = _data_of(response.text, "completed")
    assert completed["characterImages"] == [image]
    assert f'[[{image["imageUrl"]}]]' in completed["aiOutput"]
    assert "parent.webp" not in completed["aiOutput"]
    child.assert_awaited_once()


@pytest.mark.parametrize("legacy_flag", [False, True])
@pytest.mark.parametrize("slots", [None, []])
async def test_child_image_disabled_without_slots(client, mock_events, monkeypatch, legacy_flag, slots) -> None:
    from unittest.mock import AsyncMock
    from src.services import chat_child_image

    child = AsyncMock()
    monkeypatch.setattr(chat_child_image, "generate_child_image", child)
    parent = {"name": "레이", "image_name": "레이_기본", "image_url": "https://cdn.manyak.app/parent.webp"}
    mock_events([
        {"event": "character_image", **parent},
        {"event": "token", "text": "레이: 안녕."},
        {"event": "completed", "ai_output": "[[https://cdn.manyak.app/parent.webp]]\n레이: 안녕.",
         "character_images": [parent]},
    ])
    payload = {**_payload(), "generate_child_image": legacy_flag, "character_images": [parent]}
    if slots is not None:
        payload["image_slots"] = slots
    response = await client.post("/api/v1/chat/turns", json=payload)
    assert response.status_code == 200
    child.assert_not_awaited()
    assert "generatedImage" not in response.text
    assert _data_of(response.text, "character_image")["imageUrl"] == parent["image_url"]
    completed = _data_of(response.text, "completed")
    assert parent["image_url"] in completed["aiOutput"]
    assert completed["characterImages"][0]["imageUrl"] == parent["image_url"]


async def test_disconnect_cancels_the_image_call(monkeypatch, mock_events) -> None:
    from contextlib import aclosing
    from src.services import chat_child_image

    stopped_image = asyncio.Event()
    async def image(inputs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped_image.set()
    monkeypatch.setattr(chat_child_image, "generate_child_image", image)
    monkeypatch.setattr(chat_child_image, "_PING_INTERVAL_SECONDS", 0.01)
    mock_events([{"event": "completed", "ai_output": "레이: 안녕."}])
    payload = _payload()
    payload.update(image_slots=_image_slots(), character_images=[{
        "name": "레이", "image_name": "레이_기본", "image_url": "https://cdn.manyak.app/parent.webp",
    }])
    async with aclosing(chat_module._event_stream(ChatTurnRequest(**payload), {})) as stream:
        assert "event: ping" in await anext(stream)
    assert stopped_image.is_set()


@pytest.mark.parametrize("during_send", [False, True])
async def test_http_disconnect_waits_for_image_cleanup(monkeypatch, mock_events, during_send) -> None:
    from src.services import chat_child_image

    stopped = set()
    async def work(name):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            stopped.add(name)
    async def image(inputs):
        return await work("image")
    monkeypatch.setattr(chat_child_image, "generate_child_image", image)
    monkeypatch.setattr(chat_child_image, "_PING_INTERVAL_SECONDS", 0.01)
    mock_events([{"event": "completed", "ai_output": "레이: 안녕."}])
    payload = _payload()
    payload.update(image_slots=_image_slots(), character_images=[{
        "name": "레이", "image_name": "레이_기본", "image_url": "https://cdn.manyak.app/parent.webp",
    }])
    disconnect = asyncio.Event()
    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        if message["type"] == "http.response.body" and b"event: ping" in message.get("body", b""):
            disconnect.set()
            if during_send:
                # 전송 도중 끊기면 다음 anext()가 호출되지 않는다.
                await asyncio.Event().wait()
    response = await chat_module.chat_turn(ChatTurnRequest(**payload))
    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 1)
    assert stopped == {"image"}


@pytest.mark.parametrize("error", [None, "timeout", "rate_limited", "rejected", "generation_failed"])
async def test_child_image_trace_records_result_without_raw_data(monkeypatch, mock_events, error) -> None:
    from contextlib import contextmanager
    from unittest.mock import AsyncMock
    from src.core.langfuse import _Trace
    from src.services import chat_child_image
    from src.services.image.generate_child import ChildImageResult

    trace = _Trace()
    captured = {}
    @contextmanager
    def observe(*args, **kwargs):
        captured.update(kwargs)
        yield trace
    monkeypatch.setattr(chat_module, "observe_request", observe)
    monkeypatch.setattr(chat_child_image, "generate_child_image", AsyncMock(return_value=ChildImageResult(
        "레이", "private-image-name", None if error else "private-base64", error=error,
    )))
    mock_events([{"event": "completed", "ai_output": "레이: private-dialogue"}])
    payload = _payload()
    payload.update(image_slots=_image_slots(), character_images=[{
        "name": "레이", "image_name": "레이_기본", "image_url": "https://cdn.manyak.app/private-parent.webp",
    }])
    result = [event async for event in chat_module._event_stream(ChatTurnRequest(**payload), {})]
    assert "event: completed" in result[-1]
    assert "image_slots" not in captured["input_data"]
    assert payload["image_slots"][0]["upload_url"] not in str(captured)
    assert captured["input_data"]["user_input"] == payload["user_input"]
    record = trace._metadata["child_image"]
    assert record["status"] == ("failed" if error else "success")
    assert record["reason"] == error
    assert record["parent_fallback"] is bool(error)
    assert record["duration_ms"] >= 0
    assert record["prompt_version"] >= 1
    assert "private" not in str(record) and "레이" not in str(record)
    assert "cost" not in record and "usage" not in record  # 이미지 generation에만 기록한다.


@pytest.mark.parametrize("outcome", ["success", "download_failed", "rate_limited", "timeout", "invalid_image"])
async def test_child_image_from_body_sdk_through_edit_http_to_sse(
    client, monkeypatch, install_llm_sdk, outcome,
) -> None:
    """본문 SDK 응답과 외부 HTTP만 대체해 부모 선택부터 최종 SSE까지 연결한다."""
    from src.core.config import Settings, settings
    from src.services.image import generate_child, openai_api

    body = "*문이 열린다.*\n행인: 누구세요?\n레이: 들어와.\n레이: 앉아."
    streams = []

    async def create(**kwargs):
        assert kwargs["stream"] is True  # 판정 재료가 없으므로 별도 LLM 호출은 생략한다.
        stream = FakeStream([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])
            for part in [body[:18], body[18:]]
        ])
        streams.append(stream)
        return stream

    install_llm_sdk(create)
    monkeypatch.setattr(settings, "image_model", Settings.model_fields["image_model"].default)
    monkeypatch.setattr(settings, "image_parent_allowed_hosts", ["cdn.manyak.app"])
    parent = b"\x89PNG\r\n\x1a\nparent"
    child_base64 = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPchild").decode()
    downloads, edits, validated_outcomes = [], [], []

    def download(request):
        downloads.append(request)
        assert str(request.url) == "https://cdn.manyak.app/parent.png"
        return httpx.Response(404 if outcome == "download_failed" else 200, content=parent)

    def edit(request):
        edits.append(request)
        assert request.url.path == "/v1/images/edits"
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content,
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in message.iter_parts()
        }
        assert fields["model"] == b"gpt-image-2.5-flare"
        assert fields["image"] == parent
        prompt = fields["prompt"].decode()
        expected_dialogue = (
            "## dialogue_input\n\ntarget_character:\n> 레이\n\n"
            "### recent_turns\n\n"
            "#### turn (relative_to_current: -2)\n\n"
            "user_message:\n> 최근1\n\nai_response:\n> 레이: 답1\n\n"
            "#### turn (relative_to_current: -1)\n\n"
            "user_message:\n> 최근2\n\nai_response:\n> 레이: 답2\n\n"
            "### current_turn\n\n"
            "user_message:\n> 용건이 뭐요?\n\n"
            "ai_response:\n> *문이 열린다.*\n> 행인: 누구세요?\n> 레이: 들어와."
        )
        assert expected_dialogue in prompt
        assert "레이: 앉아." not in prompt
        assert "제외할 과거" not in prompt
        assert "https://cdn.manyak.app/old.webp" not in prompt
        # 콜백의 단언 실패도 서비스가 부모 대체로 처리하므로, 바깥에서 도달 횟수를 확인한다.
        validated_outcomes.append(outcome)
        if outcome == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if outcome == "rate_limited":
            return httpx.Response(429, json={"error": {"message": "test limit"}})
        data = "invalid!" if outcome == "invalid_image" else child_base64
        return httpx.Response(200, json={"created": 0, "data": [{"b64_json": data}]})

    client_type = httpx.AsyncClient
    async with AsyncOpenAI(
        api_key="test-key", http_client=client_type(transport=httpx.MockTransport(edit)),
    ) as image_client:
        monkeypatch.setattr(openai_api, "_client", lambda *args: image_client)
        # httpx 모듈 전체를 바꾸면 OpenAI SDK의 클라이언트 타입 검사도 영향을 받는다.
        monkeypatch.setattr(generate_child, "httpx", SimpleNamespace(**{
            **vars(httpx),
            "AsyncClient": lambda **kwargs: client_type(transport=httpx.MockTransport(download), **kwargs),
        }))
        payload = _payload()
        payload.update(image_slots=_image_slots(), character_images=[{
            "name": "레이", "image_name": "레이_기본", "image_url": "https://cdn.manyak.app/parent.png",
        }])
        payload["history"] = [{"role": "ASSISTANT", "content": "오프닝"}]
        for user, answer in [("제외할 과거", "옛 답"), ("최근1", "레이: 답1"), ("최근2", "레이: 답2")]:
            payload["history"].extend([
                {"role": "USER", "content": user},
                {"role": "ASSISTANT", "content": "[[https://cdn.manyak.app/old.webp]]\n\n" + answer},
            ])
        original = json.dumps(payload, ensure_ascii=False)
        names = []
        for _ in range(2):  # 같은 입력을 재생성해도 자식 이름과 요청 상태를 공유하지 않는다.
            response = await client.post("/api/v1/chat/turns", json=payload)
            assert response.status_code == 200
            image = _data_of(response.text, "character_image")
            names.append(image["imageName"])
            assert image["name"] == "레이"
            expected_url = payload["image_slots"][0]["public_url"] if outcome == "success" else "https://cdn.manyak.app/parent.png"
            assert image["imageUrl"] == expected_url
            if outcome != "success":
                assert image["imageName"] == "레이_기본"
            assert "generatedImage" not in response.text
            assert response.text.count("event: character_image") == 1
            assert response.text.index("event: character_image") < response.text.index("레이: 들어와")
            completed = _data_of(response.text, "completed")
            assert completed["characterImages"] == [{k: v for k, v in image.items() if k != "generatedImage"}]
            assert completed["aiOutput"].count(f"[[{expected_url}]]") == 1
            assert completed["aiOutput"].endswith("레이: 앉아.")
            assert "generatedImage" not in json.dumps(completed)
            assert child_base64 not in response.text
            assert "event: error" not in response.text
        if outcome == "success":
            assert names[0] != names[1]
        assert json.dumps(payload, ensure_ascii=False) == original
        assert len(downloads) == 2
        assert len(edits) == (0 if outcome == "download_failed" else 2)  # SDK 자동 재시도 없음
        assert validated_outcomes == ([] if outcome == "download_failed" else [outcome, outcome])
        assert len(streams) == 2 and all(stream.closed for stream in streams)


async def test_off_selects_both_speakers_and_stores_same_images(client, mock_events, monkeypatch):
    from unittest.mock import AsyncMock
    from src.services import chat_selected_images
    from src.services.chat_image_selection import ImageSelectionResult
    from src.schemas.chat_turn import CharacterImageMapping

    selected = [CharacterImageMapping(name=name, image_name=f"{name}_웃음", image_url=f"https://cdn.example/{name}_웃음")
                for name in ("세린", "민수")]
    select = AsyncMock(return_value=ImageSelectionResult(images=selected))
    monkeypatch.setattr(chat_selected_images, "select_images", select)
    monkeypatch.setattr(chat_selected_images, "_TEXT_CHUNK_INTERVAL_SECONDS", 0)
    text = "세린: 반가워.\n민수: 나도."
    mock_events([{"event": "token", "text": text},
                 {"event": "completed", "ai_output": text, "model": "deepseek-flash", "provider": "deepseek"}])
    payload = _payload() | {"image_slots": [], "character_images": [
        image.model_dump() for image in selected
    ]}
    response = await client.post("/api/v1/chat/turns", json=payload)
    assert response.status_code == 200
    select.assert_awaited_once()
    frames = [frame.splitlines() for frame in response.text.strip().split("\n\n")]
    events = [(lines[0][7:], json.loads(lines[1][6:])) for lines in frames]
    images = [data for event, data in events if event == "character_image"]
    assert [data["name"] for data in images] == ["세린", "민수"]
    assert all(len(data["text"]) <= 5 for event, data in events if event == "token")
    assert "".join(data["text"] for event, data in events if event == "token") == text
    completed = _data_of(response.text, "completed")
    assert completed["characterImages"] == images
    assert all(f"[[{image.image_url}]]" in completed["aiOutput"] for image in selected)


async def test_chat_prompt_records_same_version_in_response_and_trace(
    client, mock_events, monkeypatch
) -> None:
    """응답과 관측에 동일한 CHAT 버전을 기록한다."""
    from contextlib import contextmanager

    captured: dict = {}

    @contextmanager
    def _fake_observe(name, **kwargs):
        captured.update(kwargs)

        class _Trace:
            def set_metadata(self, **kw) -> None: ...

        yield _Trace()

    monkeypatch.setattr(chat_module, "observe_request", _fake_observe)
    mock_events([{"event": "completed", "ai_output": "장면", "model": "m", "provider": "p"}])

    resp = await client.post("/api/v1/chat/turns", json=_payload())

    expected = {"CHAT": 1}
    assert _data_of(resp.text, "completed")["meta"]["promptVersions"] == expected
    assert captured["metadata"]["prompt_versions"] == expected
