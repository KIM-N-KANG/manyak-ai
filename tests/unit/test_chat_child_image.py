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
        user_input="안녕", image_slots=[dict(key="test.webp", upload_url="https://bucket.s3.amazonaws.com/test.webp", public_url="https://cdn.manyak.app/test.webp")],
        character_images=[image("라떼"), image("모카"), image("라떼", "웃음")],
    )


async def events(text: str):
    yield {"event": "token", "text": text}
    yield {"event": "completed", "ai_output": text, "character_images": [], "model": "test", "provider": "openai"}


@pytest.fixture(autouse=True)
def uploaded(monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "image_upload_allowed_hosts", ["bucket.s3.amazonaws.com"])
    async def upload(child, slot):
        from dataclasses import replace
        return child if child.error else replace(child, image_url=slot.public_url)
    monkeypatch.setattr(service, "upload_child_image", upload)


async def run(req: ChatTurnRequest, text: str) -> list[dict]:
    return [event async for event in service.stream_with_child_image(events(text), req, deadline=time.monotonic() + 5)]


@pytest.mark.parametrize("allowed_hosts", [[], ["other.s3.amazonaws.com"]])
async def test_invalid_upload_target_skips_generation_and_keeps_parent(monkeypatch, request_data, allowed_hosts):
    from src.core.config import settings

    monkeypatch.setattr(settings, "image_upload_allowed_hosts", allowed_hosts)
    generate, upload = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, "generate_child_image", generate)
    monkeypatch.setattr(service, "upload_child_image", upload)
    observation = service.ChildImageObservation()
    result = [event async for event in service.stream_with_child_image(
        events("라떼: 안녕."), request_data, deadline=time.monotonic() + 5, observation=observation,
    )]
    generate.assert_not_awaited()
    upload.assert_not_awaited()
    assert result[0]["image_url"] == request_data.character_images[0].image_url
    assert result[-1]["event"] == "completed"
    assert request_data.character_images[0].image_url in result[-1]["ai_output"]
    assert result[-1]["character_images"][0]["image_name"] == "라떼_기본"
    assert observation.reason == "invalid_upload_url" and observation.parent_fallback


async def test_child_once_at_first_eligible_speaker_and_full_current_turn(monkeypatch, request_data) -> None:
    text = "*문이 열린다.*\n행인: 안녕.\n라떼: 반가워.\n모카: 들어와.\n라떼: 앉아."
    generate = AsyncMock(return_value=ChildImageResult("라떼", "라떼_실시간_test", "base64"))
    monkeypatch.setattr(service, "generate_child_image", generate)
    result = await run(request_data, text)
    first_image = next(i for i, event in enumerate(result) if event["event"] == "character_image")
    assert "".join(event["text"] for event in result[:first_image]) == "*문이 열린다.*\n행인: 안녕.\n"
    assert "generated_image" not in result[first_image]
    assert result[first_image]["image_name"] == "라떼_실시간_test"
    assert result[first_image + 1]["text"].startswith("라떼:")
    images = [e for e in result if e["event"] == "character_image"]
    assert [e["name"] for e in images] == ["라떼", "모카"]
    assert "generated_image" not in images[1]
    assert result[-1]["ai_output"].count(f"[[{request_data.image_slots[0].public_url}]]") == 1
    assert "base64" not in str(result[-1])
    generate.assert_awaited_once()
    assert generate.call_args.args[0].current_turn.ai_response == text
    assert generate.call_args.args[0].parent_image.image_name == "라떼_기본"


@pytest.mark.parametrize("error", ["timeout", "rate_limited", "rejected", "generation_failed"])
async def test_generation_failure_keeps_parent_and_completes(monkeypatch, request_data, error) -> None:
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(return_value=ChildImageResult("라떼", "라떼_실시간_test", error=error)))
    result = await run(request_data, "라떼: 안녕.")
    assert result[0]["image_name"] == "라떼_기본"
    assert "generated_image" not in result[0]
    assert result[-1]["event"] == "completed"
    assert result[-1]["character_images"][0]["image_name"] == "라떼_기본"


@pytest.mark.parametrize("status", [204, 403, 500])
async def test_real_upload_result_reaches_chat_completion(monkeypatch, request_data, status) -> None:
    import base64
    import httpx
    from src.core.config import settings
    from src.services.image import upload_child

    requests = []
    def send(request):
        requests.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(settings, "image_upload_allowed_hosts", ["bucket.s3.amazonaws.com"])
    monkeypatch.setattr(upload_child.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(send))
    monkeypatch.setattr(service, "upload_child_image", upload_child.upload_child_image)
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(return_value=ChildImageResult(
        "라떼", "라떼_실시간_test", base64.b64encode(b"image-bytes").decode(),
    )))
    result = await run(request_data, "라떼: 안녕.")
    assert len(requests) == 1
    assert result[-1]["event"] == "completed"
    child = result[0]
    if status == 204:
        assert child["image_url"] == request_data.image_slots[0].public_url
        assert "generated_image" not in child
    else:
        assert "generated_image" not in child
        assert result[0]["image_url"] == request_data.character_images[0].image_url
        assert result[-1]["character_images"][0]["image_name"] == "라떼_기본"
        assert request_data.character_images[0].image_url in result[-1]["ai_output"]


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
    assert "generated_image" not in images[0]
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
    monkeypatch.setattr(service, "_IMAGE_BUDGET_SECONDS", 0.02)
    result = [e async for e in service.stream_with_child_image(events("라떼: 안녕."), request_data, deadline=time.monotonic()+5)]
    assert cancelled.is_set()
    assert result[0]["image_name"] == "라떼_기본"
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
    assert first[0]["name"] == "라떼"
    assert second[0]["name"] == "모카"


async def test_child_replaces_only_selected_character_with_shared_parent_url(monkeypatch, request_data):
    shared = "https://cdn.manyak.app/shared.webp"
    request_data.character_images = [
        CharacterImageMapping(name=name, image_name=f"{name}_기본", image_url=shared)
        for name in ("라떼", "모카")
    ]
    original = request_data.model_dump()
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(return_value=ChildImageResult(
        "라떼", "라떼_실시간_test", "private-base64",
    )))
    result = await run(request_data, "*문이 열린다.*\n라떼: 안녕.\n모카: 반가워.\n라떼: 들어와.")
    images = [event for event in result if event["event"] == "character_image"]
    completed = result[-1]
    assert [item["image_url"] for item in images] == [request_data.image_slots[0].public_url, shared]
    assert completed["character_images"] == [{key: value for key, value in event.items() if key != "event"} for event in images]
    assert completed["ai_output"].count(f"[[{shared}]]") == 1
    assert completed["ai_output"].count(f"[[{request_data.image_slots[0].public_url}]]") == 1
    assert "private-base64" not in str(result) and "generated_image" not in str(result)
    assert request_data.model_dump() == original


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
    assert result[0]["image_name"] == "라떼_기본"
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
        assert result["image_url"] == request_data.image_slots[0].public_url
        assert "generated_image" not in result


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


@pytest.mark.parametrize("failure", [None, "generation_failed", "upload_failed", "timeout"])
async def test_waits_for_image_before_scene_then_streams_in_order(monkeypatch, request_data, failure):
    release = asyncio.Event()
    finished = asyncio.Event()
    async def generate(inputs):
        try:
            await release.wait()
            return ChildImageResult("라떼", "라떼_실시간_test", "data", error=(
                "generation_failed" if failure == "generation_failed" else None
            ))
        finally:
            finished.set()
    monkeypatch.setattr(service, "generate_child_image", generate)
    monkeypatch.setattr(service, "_PING_INTERVAL_SECONDS", 0.01)
    if failure == "timeout":
        monkeypatch.setattr(service, "_IMAGE_BUDGET_SECONDS", 0.05)
    if failure == "upload_failed":
        monkeypatch.setattr(service, "upload_child_image", AsyncMock(return_value=ChildImageResult(
            "라떼", "라떼_실시간_test", error="generation_failed",
        )))
    text = "*문이 열리고 조용한 방 안으로 들어선다.*\n라떼: 어서 와. 여기 앉아서 이야기해.\n*바람이 창문을 두드린다.*"
    received = []
    async with aclosing(service.stream_with_child_image(
        events(text), request_data, deadline=time.monotonic()+5,
    )) as stream:
        # 본문은 이미 완성됐지만 이미지 대기 중에는 첫 장면도 보내면 안 된다.
        assert (await anext(stream))["event"] == "ping"
        assert not finished.is_set()
        if failure != "timeout":
            release.set()
        async for event in stream:
            if event["event"] != "ping":
                assert finished.is_set()
                received.append((time.monotonic(), event))
    tokens = [(at, e) for at, e in received if e["event"] == "token"]
    assert len(tokens) >= 5
    assert all(0 < len(e["text"]) <= service._TEXT_CHUNK_CHARACTERS for _, e in tokens)
    assert all(b[0] - a[0] >= 0.02 for a, b in zip(tokens, tokens[1:]))
    assert "".join(e["text"] for _, e in tokens) == text
    output = [e for _, e in received]
    index = next(i for i, e in enumerate(output) if e["event"] == "character_image")
    assert "".join(e["text"] for e in output[:index]) == text.split("라떼:")[0]
    assert output[index + 1]["text"].startswith("라떼:")
    expected_url = request_data.character_images[0].image_url if failure else request_data.image_slots[0].public_url
    assert output[index]["image_url"] == expected_url
    assert output[-1]["event"] == "completed"
    assert expected_url in output[-1]["ai_output"]


async def test_replay_deadline_does_not_flush_remaining_text():
    result = [e async for e in service._stream_rendered(
        [{"event": "token", "text": "아직 보내지 않은 글"}], deadline=time.monotonic()-1,
    )]
    assert [e["event"] for e in result] == ["error"]


async def test_image_timeout_leaves_time_for_parent_and_text(monkeypatch, request_data):
    cancelled = asyncio.Event()
    async def generate(inputs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(service, "generate_child_image", generate)
    deadline = time.monotonic() + 2.0
    result = [e async for e in service.stream_with_child_image(
        events("*문이 열린다.*\n라떼: 반가워."), request_data, deadline=deadline,
    )]
    assert cancelled.is_set()
    assert result[-1]["event"] == "completed"
    assert time.monotonic() < deadline
    assert next(e for e in result if e["event"] == "character_image")["image_name"] == "라떼_기본"


async def test_replay_respects_total_pacing_budget(monkeypatch):
    from types import SimpleNamespace

    now, waits = [0.0], []
    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    text = "가" * 12000
    result = [e async for e in service._stream_rendered(
        [{"event": "token", "text": text}], deadline=100,
    )]
    assert "".join(e["text"] for e in result) == text
    assert 0 < sum(waits) <= service._TEXT_REPLAY_BUDGET_SECONDS + 0.00001


async def test_disconnect_during_replay_stops_delivery(monkeypatch, request_data):
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(return_value=ChildImageResult(
        "라떼", "라떼_실시간_test", "data",
    )))
    async with aclosing(service.stream_with_child_image(
        events("*긴 장면이 이어진다.*\n라떼: 안녕."), request_data, deadline=time.monotonic()+5,
    )) as stream:
        assert (await anext(stream))["event"] == "token"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
