"""완성된 채팅 본문으로 자식 이미지를 만들고 첫 대사 앞 이벤트에 첨부한다."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from uuid import uuid4

import httpx
from anyio import CancelScope

from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE, EVENT_COMPLETED, EVENT_ERROR, EVENT_PING, EVENT_TOKEN, CharacterImageMapping, ChatImageSlot, ChatTurnRequest,
)
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.chat_llm import render_chat_images
from src.services.image.child_input import ChildImageInput, build_child_image_input
from src.services.image.child_prompt import CHILD_IMAGE_VERSION
from src.services.image.generate_child import ChildImageResult, generate_child_image
from src.services.image.upload_child import upload_child_image, validate_upload_url

_PING_INTERVAL_SECONDS = 10.0
_IMAGE_BUDGET_SECONDS = 30.0
_TEXT_CHUNK_CHARACTERS = 12
_TEXT_CHUNK_INTERVAL_SECONDS = 0.03
_TEXT_REPLAY_BUDGET_SECONDS = 8.0


async def _stream_rendered(events: list[dict], *, deadline: float) -> AsyncIterator[dict]:
    """이미지 위치를 보존하며 완성된 글을 나눠 보낸다. 완료 여유 시간은 쓰지 않는다."""
    chunks = sum(
        (len(event["text"]) + _TEXT_CHUNK_CHARACTERS - 1) // _TEXT_CHUNK_CHARACTERS
        for event in events if event["event"] == EVENT_TOKEN
    )
    # 긴 본문도 고정 속도로 보내다 턴 상한을 넘기지 않게 남은 시간 절반 이내로 잡는다.
    interval = min(
        _TEXT_CHUNK_INTERVAL_SECONDS,
        _TEXT_REPLAY_BUDGET_SECONDS / max(chunks, 1),
        max(0.0, deadline - time.monotonic()) / (2 * max(chunks, 1)),
    )
    for event in events:
        parts = (
            [{**event, "text": event["text"][offset:offset + _TEXT_CHUNK_CHARACTERS]}
             for offset in range(0, len(event["text"]), _TEXT_CHUNK_CHARACTERS)]
            if event["event"] == EVENT_TOKEN else [event]
        )
        for part in parts:
            if part["event"] == EVENT_TOKEN:
                await asyncio.sleep(interval)
            if time.monotonic() >= deadline:
                yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": "채팅 응답 대기 시간이 초과됐습니다."}
                return
            yield part


@dataclass
class ChildImageObservation:
    """채팅 트레이스에 붙일 비원문 결과. 이미지·인물 이름·URL은 기록하지 않는다."""

    status: str = "not_started"
    reason: str | None = "body_incomplete"
    duration_ms: float | None = None
    parent_fallback: bool = False
    prompt_version: int = CHILD_IMAGE_VERSION


async def _collect(events: AsyncIterator[dict]) -> dict:
    async with aclosing(events):
        async for event in events:
            if event["event"] in (EVENT_COMPLETED, EVENT_ERROR):
                return event
    return {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": "채팅 연동 중 오류가 발생했습니다."}


async def _pings_until_done(task: asyncio.Task) -> AsyncIterator[dict]:
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=_PING_INTERVAL_SECONDS)
            if not done:
                yield {"event": EVENT_PING}
    finally:
        # HTTP 연결 종료의 반복 취소로 정리 작업까지 끊기지 않게 한다.
        with CancelScope(shield=True):
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _generate_before_deadline(
    inputs: ChildImageInput, deadline: float, observation: ChildImageObservation, slot: ChatImageSlot,
) -> ChildImageResult:
    async def generate() -> ChildImageResult:
        result = await generate_child_image(inputs)
        if time.monotonic() > deadline:
            raise TimeoutError("자식 이미지 생성 시간 초과")
        result = await upload_child_image(result, slot)
        # 완료 시각을 검사해 취소를 무시하고 늦게 반환한 결과도 거른다.
        if time.monotonic() > deadline:
            raise TimeoutError("자식 이미지 생성 시간 초과")
        return result

    started = time.monotonic()
    observation.status, observation.reason = "running", None
    try:
        try:
            validate_upload_url(slot)
        except (ValueError, httpx.InvalidURL):
            observation.status, observation.reason = "skipped", "invalid_upload_url"
            return ChildImageResult(
                name=inputs.parent_image.name,
                image_name=f"{inputs.parent_image.name}_실시간_{uuid4()}",
                error="generation_failed",
            )
        result = await asyncio.wait_for(generate(), timeout=deadline - started)
        observation.status = "failed" if result.error else "success"
        observation.reason = result.error
        return result
    except TimeoutError:
        observation.status, observation.reason = "failed", "timeout"
        raise
    except asyncio.CancelledError:
        # wait_for 바깥의 취소는 요청 종료다. 정리가 늦어져도 시간 초과로 바꾸지 않는다.
        observation.status, observation.reason = "cancelled", "cancelled"
        raise
    except Exception:
        # 이미지 내부 오류도 채팅 완료를 막지 않는다. 원문 대신 고정된 분류만 기록한다.
        observation.status, observation.reason = "failed", "unexpected_error"
        return ChildImageResult(
            name=inputs.parent_image.name,
            image_name=f"{inputs.parent_image.name}_실시간_{uuid4()}",
            error="generation_failed",
        )
    finally:
        observation.duration_ms = round((time.monotonic() - started) * 1000, 2)


async def stream_with_child_image(
    events: AsyncIterator[dict], req: ChatTurnRequest, *, deadline: float,
    on_body_completed: Callable[[str], None] | None = None,
    observation: ChildImageObservation | None = None,
) -> AsyncIterator[dict]:
    """본문·이미지 결과를 확보한 뒤 지문 → 이미지 → 대사를 순차 스트리밍한다.

    업로드 성공 시 해당 인물의 이벤트·본문·목록에 자식 주소를 반영한다.
    실패 시 부모를 유지하며 이미지 데이터는 응답에 싣지 않는다.
    """
    if observation is None:
        observation = ChildImageObservation()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        observation.status, observation.reason = "skipped", "body_timeout"
        await events.aclose()
        yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": "채팅 응답 대기 시간이 초과됐습니다."}
        return
    # 이미지 시간 초과 후에도 부모 이미지와 글을 순차 전송할 시간을 남긴다.
    replay_reserve = min(_TEXT_REPLAY_BUDGET_SECONDS, remaining / 4)
    work_deadline = deadline - replay_reserve
    collecting = asyncio.create_task(asyncio.wait_for(_collect(events), timeout=remaining - replay_reserve))
    async with aclosing(_pings_until_done(collecting)) as waiting:
        async for ping in waiting:
            yield ping
    try:
        completed = collecting.result()
    except TimeoutError:
        observation.status, observation.reason = "skipped", "body_timeout"
        yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": "채팅 응답 대기 시간이 초과됐습니다."}
        return
    if completed["event"] == EVENT_ERROR:
        observation.status, observation.reason = "skipped", "body_error"
        yield completed
        return

    text = strip_character_image_syntax(completed["ai_output"])
    if on_body_completed is not None:
        on_body_completed(text)
    inputs = build_child_image_input(
        character_images=req.character_images, history=req.history,
        user_input=req.user_input, ai_output=text,
    )
    if inputs is None:
        observation.status, observation.reason = "skipped", "no_parent"
    rendered, stored, displayed = render_chat_images(text, req.character_images)
    generating = None
    image_deadline = min(work_deadline, time.monotonic() + _IMAGE_BUDGET_SECONDS)
    if inputs is not None and image_deadline > time.monotonic():
        generating = asyncio.create_task(
            _generate_before_deadline(inputs, image_deadline, observation, req.image_slots[0]),
        )
    try:
        child = None
        if generating is not None:
            async with aclosing(_pings_until_done(generating)) as waiting:
                async for ping in waiting:
                    yield ping
            try:
                child = generating.result()
            except TimeoutError:
                child = None
        for event in rendered:
            if (
                inputs is not None and event["event"] == EVENT_CHARACTER_IMAGE
                and event["name"] == inputs.parent_image.name
            ):
                if child is None:
                    observation.status, observation.reason = "failed", "timeout"
                    child = ChildImageResult(
                        name=inputs.parent_image.name,
                        image_name=f"{inputs.parent_image.name}_실시간_{uuid4()}", error="timeout",
                    )
                observation.parent_fallback = child.error is not None or not child.image_url
                if not observation.parent_fallback:
                    event.update(image_name=child.image_name, image_url=child.image_url)
                    # URL 전체 치환은 같은 부모 URL을 쓰는 다른 인물까지 바꾼다.
                    # 대상 인물의 매핑만 교체해 기존 마커 생성 규칙으로 본문을 다시 만든다.
                    mappings = [
                        CharacterImageMapping(name=item.name, image_name=child.image_name, image_url=child.image_url)
                        if item.name == inputs.parent_image.name else item
                        for item in req.character_images
                    ]
                    _, stored, displayed = render_chat_images(text, mappings)
        async with aclosing(_stream_rendered(rendered, deadline=deadline)) as replay:
            async for event in replay:
                yield event
                if event["event"] == EVENT_ERROR:
                    return
        yield {**completed, "ai_output": stored, "character_images": displayed}
    finally:
        if generating is not None:
            with CancelScope(shield=True):
                if not generating.done() and not generating.cancelling():
                    generating.cancel()
                await asyncio.gather(generating, return_exceptions=True)
