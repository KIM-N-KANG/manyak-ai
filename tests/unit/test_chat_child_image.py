"""KNK-1266: 본문·이미지 순서, 부모 대체, 취소와 요청 간 분리를 검증한다."""

import asyncio
import time
from contextlib import aclosing
from unittest.mock import AsyncMock

import pytest

from src.schemas.chat_turn import CharacterImageMapping, ChatTurnRequest
from src.services import chat_child_image as service
from src.services.chat_llm import render_chat_images
from src.services.image.generate_child import ChildImageResult


def image(name: str, suffix: str = "기본") -> CharacterImageMapping:
    return CharacterImageMapping(name=name, image_name=f"{name}_{suffix}", image_url=f"https://cdn.manyak.app/{name}_{suffix}.webp")


@pytest.fixture
def request_data() -> ChatTurnRequest:
    return ChatTurnRequest(
        genre="판타지", story_settings=dict(world_setting="", character_setting="", user_role_setting="", rule_setting=""),
        start_settings=dict(name="", prologue="", start_situation=""), summary="",
        user_input="안녕", generate_child_image=True,
        character_images=[image("라떼"), image("모카"), image("라떼", "웃음")],
    )


async def events(text: str):
    yield {"event": "token", "text": text}
    yield {"event": "completed", "ai_output": text, "character_images": [], "model": "test", "provider": "openai"}


async def run(req: ChatTurnRequest, text: str) -> list[dict]:
    return [event async for event in service.stream_with_child_image(events(text), req, deadline=time.monotonic() + 5)]


async def test_child_once_at_first_eligible_speaker_and_full_current_turn(monkeypatch, request_data) -> None:
    text = "*문이 열린다.*\n행인: 안녕.\n라떼: 반가워.\n모카: 들어와.\n라떼: 앉아."
    generate = AsyncMock(return_value=ChildImageResult("라떼", "라떼_실시간_test", "base64"))
    monkeypatch.setattr(service, "generate_child_image", generate)
    result = await run(request_data, text)
    assert result[0] == {"event": "token", "text": "*문이 열린다.*\n행인: 안녕.\n"}
    assert result[1]["generated_image"]["image_base64"] == "base64"
    assert result[1]["image_name"] == "라떼_기본"
    assert result[2]["text"].startswith("라떼:")
    images = [e for e in result if e["event"] == "character_image"]
    assert [e["name"] for e in images] == ["라떼", "모카"]
    assert "generated_image" not in images[1]
    assert result[-1]["ai_output"].count("[[https://cdn.manyak.app/라떼_기본.webp]]") == 1
    assert "base64" not in str(result[-1])
    generate.assert_awaited_once()
    assert generate.call_args.args[0].current_turn.ai_response == text
    assert generate.call_args.args[0].parent_image.image_name == "라떼_기본"


@pytest.mark.parametrize("error", ["timeout", "rate_limited", "rejected", "generation_failed"])
async def test_generation_failure_keeps_parent_and_completes(monkeypatch, request_data, error) -> None:
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(return_value=ChildImageResult("라떼", "라떼_실시간_test", error=error)))
    result = await run(request_data, "라떼: 안녕.")
    assert result[0]["image_name"] == "라떼_기본"
    assert result[0]["generated_image"]["error"] == error
    assert result[-1]["event"] == "completed"
    assert result[-1]["character_images"][0]["image_name"] == "라떼_기본"


@pytest.mark.parametrize("exception", [RuntimeError, ValueError])
async def test_unexpected_image_error_keeps_parent_and_completes(monkeypatch, request_data, exception) -> None:
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(side_effect=exception("private-error-detail")))
    observation = service.ChildImageObservation()
    result = [event async for event in service.stream_with_child_image(
        events("라떼: 안녕.\n모카: 반가워."), request_data,
        deadline=time.monotonic()+5, observation=observation,
    )]
    images = [event for event in result if event["event"] == "character_image"]
    assert images[0]["image_name"] == "라떼_기본"
    assert images[0]["image_url"] == request_data.character_images[0].image_url
    assert images[0]["generated_image"]["error"] == "generation_failed"
    assert images[0]["generated_image"]["image_base64"] is None
    assert "generated_image" not in images[1]
    assert result[-1]["event"] == "completed"
    assert "라떼: 안녕." in result[-1]["ai_output"]
    assert "모카: 반가워." in result[-1]["ai_output"]
    assert result[-1]["character_images"][0]["image_name"] == "라떼_기본"
    assert (observation.status, observation.reason) == ("failed", "unexpected_error")
    assert observation.duration_ms is not None and observation.parent_fallback
    assert "private-error-detail" not in str(result) + str(observation)


@pytest.mark.parametrize("text", ["*아무도 말하지 않는다.*", "행인: 안녕."])
async def test_no_eligible_speaker_does_not_generate(monkeypatch, request_data, text) -> None:
    generate = AsyncMock()
    monkeypatch.setattr(service, "generate_child_image", generate)
    result = await run(request_data, text)
    generate.assert_not_awaited()
    assert result[-1]["ai_output"] == text


async def test_text_failure_does_not_generate(monkeypatch, request_data) -> None:
    async def failed():
        yield {"event": "token", "text": "라떼: 중간"}
        yield {"event": "error", "code": "LLM_ERROR", "message": "실패"}
    generate = AsyncMock()
    monkeypatch.setattr(service, "generate_child_image", generate)
    result = [e async for e in service.stream_with_child_image(failed(), request_data, deadline=time.monotonic()+5)]
    assert result == [{"event": "error", "code": "LLM_ERROR", "message": "실패"}]
    generate.assert_not_awaited()


async def test_expired_deadline_returns_error_without_generation(monkeypatch, request_data) -> None:
    generate = AsyncMock()
    monkeypatch.setattr(service, "generate_child_image", generate)
    result = [e async for e in service.stream_with_child_image(events("라떼: 안녕."), request_data, deadline=0)]
    generate.assert_not_awaited()
    assert result == [{"event": "error", "code": "LLM_ERROR", "message": "채팅 응답 대기 시간이 초과됐습니다."}]


async def test_timeout_cancels_generation(monkeypatch, request_data) -> None:
    cancelled = asyncio.Event()
    async def slow(inputs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(service, "generate_child_image", slow)
    result = [e async for e in service.stream_with_child_image(events("라떼: 안녕."), request_data, deadline=time.monotonic()+0.02)]
    assert cancelled.is_set()
    assert result[0]["generated_image"]["error"] == "timeout"
    assert result[-1]["event"] == "completed"


async def test_close_during_image_ping_cancels_work(monkeypatch, request_data) -> None:
    cancelled = asyncio.Event()
    async def slow(inputs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(service, "generate_child_image", slow)
    monkeypatch.setattr(service, "_PING_INTERVAL_SECONDS", 0.01)
    async with aclosing(service.stream_with_child_image(events("라떼: 안녕."), request_data, deadline=time.monotonic()+5)) as stream:
        assert (await anext(stream))["event"] == "ping"
    assert cancelled.is_set()


async def test_close_during_text_ping_closes_source(monkeypatch, request_data) -> None:
    closed = asyncio.Event()
    async def slow():
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            closed.set()
    monkeypatch.setattr(service, "_PING_INTERVAL_SECONDS", 0.01)
    async with aclosing(service.stream_with_child_image(slow(), request_data, deadline=time.monotonic()+5)) as stream:
        assert (await anext(stream))["event"] == "ping"
    assert closed.is_set()


def test_full_label_rendering_and_alias_collisions() -> None:
    mappings = [image("코드"), image("코드:제로"), image("지한결"), image("김한결", "웃음")]
    result, stored, displayed = render_chat_images("코드:제로: 안녕.\n한결: 누구?\n지한결: 나야.", mappings)
    assert [e["name"] for e in result if e["event"] == "character_image"] == ["코드:제로", "지한결"]
    assert [e["name"] for e in displayed] == ["코드:제로", "지한결"]
    assert stored.count("[[") == 2


async def test_parallel_requests_do_not_share_selected_character(monkeypatch, request_data) -> None:
    async def generate(inputs):
        await asyncio.sleep(0)
        name = inputs.parent_image.name
        return ChildImageResult(name, f"{name}_실시간_test", "data")
    monkeypatch.setattr(service, "generate_child_image", generate)
    first, second = await asyncio.gather(run(request_data, "라떼: 안녕."), run(request_data, "모카: 안녕."))
    assert first[0]["generated_image"]["name"] == "라떼"
    assert second[0]["generated_image"]["name"] == "모카"


async def test_image_cap_includes_download_and_ignores_late_result(monkeypatch, request_data) -> None:
    cancelled = asyncio.Event()
    async def stubborn(inputs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ChildImageResult("라떼", "late", "late-data")
    monkeypatch.setattr(service, "_IMAGE_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(service, "generate_child_image", stubborn)
    result = await run(request_data, "라떼: 안녕.")
    assert cancelled.is_set()
    assert result[0]["generated_image"]["error"] == "timeout"
    assert "late-data" not in str(result)
    assert result[-1]["event"] == "completed"


async def test_body_timeout_closes_source_without_starting_judgement(monkeypatch, request_data) -> None:
    from unittest.mock import Mock
    closed = asyncio.Event()
    async def slow():
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            closed.set()
    callback = Mock()
    result = [e async for e in service.stream_with_child_image(
        slow(), request_data, deadline=time.monotonic()+0.01, on_body_completed=callback,
    )]
    assert closed.is_set()
    assert result[-1]["event"] == "error"
    callback.assert_not_called()


async def test_on_time_image_survives_delayed_delivery(monkeypatch, request_data) -> None:
    from types import SimpleNamespace

    now = [100.0]
    finished = asyncio.Event()
    async def generate(inputs):
        finished.set()
        return ChildImageResult("라떼", "라떼_실시간_test", "valid-data")
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(service, "generate_child_image", generate)
    async with aclosing(service.stream_with_child_image(
        events("*문이 열린다.*\n라떼: 안녕."), request_data, deadline=205.0,
    )) as stream:
        assert (await anext(stream))["event"] == "token"
        await asyncio.wait_for(finished.wait(), 1)
        # 생성은 100초에 끝났지만, 앞 지문 전송 후 이미지 차례는 131초에 온다.
        now[0] = 131.0
        result = await anext(stream)
        assert result["event"] == "character_image"
        assert result["generated_image"]["image_base64"] == "valid-data"
        assert result["generated_image"]["error"] is None


@pytest.mark.parametrize("text,reason", [("행인: 안녕.", "no_parent"), ("*조용하다.*", "no_parent")])
async def test_observation_when_no_parent(request_data, text, reason) -> None:
    observation = service.ChildImageObservation()
    result = [e async for e in service.stream_with_child_image(
        events(text), request_data, deadline=time.monotonic()+5, observation=observation,
    )]
    assert result[-1]["event"] == "completed"
    assert (observation.status, observation.reason) == ("skipped", reason)
    assert observation.duration_ms is None and not observation.parent_fallback


async def test_observation_for_outer_timeout(monkeypatch, request_data) -> None:
    async def slow(inputs):
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "generate_child_image", slow)
    monkeypatch.setattr(service, "_IMAGE_BUDGET_SECONDS", 0.01)
    observation = service.ChildImageObservation()
    result = [e async for e in service.stream_with_child_image(
        events("라떼: 안녕."), request_data, deadline=time.monotonic()+5, observation=observation,
    )]
    assert result[-1]["event"] == "completed"
    assert (observation.status, observation.reason) == ("failed", "timeout")
    assert observation.duration_ms > 0 and observation.parent_fallback


async def test_observation_for_cancelled_generation(monkeypatch, request_data) -> None:
    async def slow(inputs):
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "generate_child_image", slow)
    monkeypatch.setattr(service, "_PING_INTERVAL_SECONDS", 0.01)
    observation = service.ChildImageObservation()
    async with aclosing(service.stream_with_child_image(
        events("라떼: 안녕."), request_data, deadline=time.monotonic()+5, observation=observation,
    )) as stream:
        assert (await anext(stream))["event"] == "ping"
    assert (observation.status, observation.reason) == ("cancelled", "cancelled")
    assert observation.duration_ms > 0 and not observation.parent_fallback


@pytest.mark.parametrize("expired", [False, True])
async def test_observation_when_body_fails(request_data, expired) -> None:
    async def failed():
        yield {"event": "error", "code": "LLM_ERROR", "message": "private error"}
    observation = service.ChildImageObservation()
    result = [e async for e in service.stream_with_child_image(
        failed(), request_data, deadline=0 if expired else time.monotonic()+5, observation=observation,
    )]
    assert result[-1]["event"] == "error"
    assert observation.status == "skipped"
    assert observation.reason == ("body_timeout" if expired else "body_error")
    assert observation.duration_ms is None and not observation.parent_fallback


async def test_user_cancellation_stays_cancelled_after_deadline(monkeypatch, request_data) -> None:
    from types import SimpleNamespace

    now = [100.0]
    async def slow(inputs):
        try:
            await asyncio.Event().wait()
        finally:
            # 129초에 사용자가 나갔지만 연결 정리는 마감(130초) 후 끝난다.
            await asyncio.sleep(0)
            now[0] = 131.0
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(service, "generate_child_image", slow)
    monkeypatch.setattr(service, "_PING_INTERVAL_SECONDS", 0.01)
    observation = service.ChildImageObservation()
    async with aclosing(service.stream_with_child_image(
        events("라떼: 안녕."), request_data, deadline=205.0, observation=observation,
    )) as stream:
        assert (await anext(stream))["event"] == "ping"
        now[0] = 129.0
    assert (observation.status, observation.reason) == ("cancelled", "cancelled")
    assert observation.duration_ms == 31000.0
    assert not observation.parent_fallback
