"""OFF 경로의 다중 인물 선택, 출력·저장 일치, 대기·취소를 검증한다."""

import asyncio
from contextlib import aclosing
from unittest.mock import AsyncMock

import pytest

from src.schemas.chat_turn import CharacterImageMapping, ChatTurnRequest
from src.services import chat_selected_images as service
from src.services import chat_image_selection as selection
from src.services.chat_image_selection import ImageSelectionResult


def image(name, suffix="기본"):
    return CharacterImageMapping(name=name, image_name=f"{name}_{suffix}", image_url=f"https://cdn.example/{name}_{suffix}")


@pytest.fixture
def req():
    return ChatTurnRequest(
        genre="판타지", story_settings=dict(world_setting="", character_setting="", user_role_setting="", rule_setting=""),
        start_settings=dict(name="", prologue="", start_situation=""), summary="", user_input="안녕",
        character_images=[image("세린"), image("세린", "편의점_고백_당황"), image("민수"), image("민수", "웃음")],
    )


@pytest.fixture(autouse=True)
def fast_replay(monkeypatch):
    monkeypatch.setattr(service, "_TEXT_CHUNK_INTERVAL_SECONDS", 0)


async def body(text):
    yield {"event": "token", "text": text}
    yield {"event": "character_image", "name": "틀린 중간 이미지"}
    yield {"event": "completed", "ai_output": text, "model": "test", "input_tokens": 12}


async def run(req, text, **kwargs):
    return [e async for e in service.stream_with_selected_images(body(text), req, **kwargs)]


async def test_images_precede_each_speaker_and_storage_matches(monkeypatch, req):
    chosen = [req.character_images[1], req.character_images[3]]
    selecting = AsyncMock(return_value=ImageSelectionResult(images=chosen))
    monkeypatch.setattr(service, "select_images", selecting)
    text = "*편의점이다.*\n세린: 갑자기 왜 그래?\n민수: 좋아하니까.\n세린: 정말?"
    completed_bodies = []
    result = await run(req, text, on_body_completed=completed_bodies.append)
    assert completed_bodies == [text]
    assert selecting.call_args.kwargs["ai_output"] == text
    assert selecting.call_args.kwargs["timeout"] == 10
    images = [e for e in result if e["event"] == "character_image"]
    assert [e["image_name"] for e in images] == [i.image_name for i in chosen]
    for event in images:
        assert result[result.index(event) + 1]["text"].startswith(event["name"] + ":")
    chunks = [e["text"] for e in result if e["event"] == "token"]
    assert all(len(chunk) <= 5 for chunk in chunks)
    assert "".join(chunks) == text
    assert result[-1]["character_images"] == [{k: v for k, v in e.items() if k != "event"} for e in images]
    for item in chosen:
        assert result[-1]["ai_output"].count(f"[[{item.image_url}]]") == 1
    assert result[-1]["input_tokens"] == 12


async def test_failed_selection_uses_default_or_first(monkeypatch, req):
    req.character_images = [image("세린", "웃음"), image("세린"), image("민수", "분노"), image("민수", "웃음")]
    monkeypatch.setattr(service, "select_images", AsyncMock(return_value=ImageSelectionResult()))
    result = await run(req, "세린: 안녕\n민수: 반가워")
    assert [e["image_name"] for e in result if e["event"] == "character_image"] == ["세린_기본", "민수_분노"]


async def test_actual_selection_timeout_falls_back_and_cancels(monkeypatch, req):
    stopped = asyncio.Event()
    async def hanging(request):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(selection.llm, "evaluate", hanging)
    monkeypatch.setattr(service, "_SELECTION_TIMEOUT_SECONDS", 0.01)
    result = await run(req, "세린: 안녕\n민수: 반가워")
    assert stopped.is_set()
    assert [e["image_name"] for e in result if e["event"] == "character_image"] == ["세린_기본", "민수_기본"]
    assert result[-1]["event"] == "completed"


async def test_replay_waits_60ms_per_chunk(monkeypatch, req):
    monkeypatch.setattr(service, "_TEXT_CHUNK_INTERVAL_SECONDS", 0.06)
    monkeypatch.setattr(service, "select_images", AsyncMock(return_value=ImageSelectionResult()))
    sleep = AsyncMock()
    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    result = await run(req, "세린: 안녕하세요.")
    chunks = [e for e in result if e["event"] == "token"]
    assert sleep.await_count == len(chunks)
    assert all(call.args == (0.06,) for call in sleep.await_args_list)


async def test_body_failure_skips_selection(monkeypatch, req):
    selecting = AsyncMock()
    monkeypatch.setattr(service, "select_images", selecting)
    async def failed():
        yield {"event": "token", "text": "보내지 않을 부분 출력"}
        yield {"event": "error", "code": "LLM_ERROR", "message": "실패"}
    result = [e async for e in service.stream_with_selected_images(failed(), req)]
    assert [e["event"] for e in result] == ["error"]
    selecting.assert_not_awaited()


async def test_disconnect_while_waiting_closes_body(monkeypatch, req):
    from src.services import chat_child_image
    monkeypatch.setattr(chat_child_image, "_PING_INTERVAL_SECONDS", 0.01)
    stopped = asyncio.Event()
    async def pending():
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            stopped.set()
    async with aclosing(service.stream_with_selected_images(pending(), req)) as stream:
        assert (await anext(stream))["event"] == "ping"
    assert stopped.is_set()
