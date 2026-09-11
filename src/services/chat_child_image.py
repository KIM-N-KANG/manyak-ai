"""완성된 채팅 본문으로 자식 이미지를 만들고 첫 대사 앞 이벤트에 첨부한다."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import asdict
from uuid import uuid4

from anyio import CancelScope

from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE, EVENT_COMPLETED, EVENT_ERROR, EVENT_PING, ChatTurnRequest,
)
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.chat_llm import render_chat_images
from src.services.image.child_input import ChildImageInput, build_child_image_input
from src.services.image.generate_child import ChildImageResult, generate_child_image

_PING_INTERVAL_SECONDS = 10.0
_IMAGE_BUDGET_SECONDS = 30.0


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


async def _generate_before_deadline(inputs: ChildImageInput, deadline: float) -> ChildImageResult:
    result = await generate_child_image(inputs)
    # 전송 시점이 아닌 생성 완료 시점으로 검사한다. 취소를 무시한 늦은 결과도 거른다.
    if time.monotonic() > deadline:
        raise TimeoutError("자식 이미지 생성 시간 초과")
    return result


async def stream_with_child_image(
    events: AsyncIterator[dict], req: ChatTurnRequest, *, deadline: float,
    on_body_completed: Callable[[str], None] | None = None,
) -> AsyncIterator[dict]:
    """본문 전체를 확보한 뒤 앞 지문 → 이미지 → 대사 → completed 순서로 전달한다.

    generated_image는 이미지 이벤트에만 한 번 싣는다. 저장 URL을 모르는 AI는 부모 URL로
    본문과 목록을 완성하며, 백엔드가 저장 성공 시 해당 인물의 마커와 목록을 함께 바꾼다.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        await events.aclose()
        yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": "채팅 응답 대기 시간이 초과됐습니다."}
        return
    collecting = asyncio.create_task(asyncio.wait_for(_collect(events), timeout=remaining))
    async with aclosing(_pings_until_done(collecting)) as waiting:
        async for ping in waiting:
            yield ping
    try:
        completed = collecting.result()
    except TimeoutError:
        yield {"event": EVENT_ERROR, "code": "LLM_ERROR", "message": "채팅 응답 대기 시간이 초과됐습니다."}
        return
    if completed["event"] == EVENT_ERROR:
        yield completed
        return

    text = strip_character_image_syntax(completed["ai_output"])
    if on_body_completed is not None:
        on_body_completed(text)
    inputs = build_child_image_input(
        character_images=req.character_images, history=req.history,
        user_input=req.user_input, ai_output=text,
    )
    rendered, stored, displayed = render_chat_images(text, req.character_images)
    generating = None
    image_deadline = min(deadline, time.monotonic() + _IMAGE_BUDGET_SECONDS)
    if inputs is not None and image_deadline > time.monotonic():
        generating = asyncio.create_task(asyncio.wait_for(
            _generate_before_deadline(inputs, image_deadline),
            timeout=image_deadline - time.monotonic(),
        ))
    try:
        for event in rendered:
            if (
                inputs is not None and event["event"] == EVENT_CHARACTER_IMAGE
                and event["name"] == inputs.parent_image.name
            ):
                child = None
                if generating is not None:
                    async with aclosing(_pings_until_done(generating)) as waiting:
                        async for ping in waiting:
                            yield ping
                    try:
                        child = generating.result()
                    except TimeoutError:
                        child = None  # 부모 대체용 timeout 결과로 만든다.
                if child is None:
                    child = ChildImageResult(
                        name=inputs.parent_image.name,
                        image_name=f"{inputs.parent_image.name}_실시간_{uuid4()}", error="timeout",
                    )
                event = {**event, "generated_image": asdict(child)}
            yield event
        yield {**completed, "ai_output": stored, "character_images": displayed}
    finally:
        if generating is not None:
            with CancelScope(shield=True):
                if not generating.done() and not generating.cancelling():
                    generating.cancel()
                await asyncio.gather(generating, return_exceptions=True)
