"""실시간 생성 OFF: 완성된 대화로 인물별 이미지를 골라 순서대로 전송한다."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing

from src.schemas.chat_turn import EVENT_ERROR, EVENT_TOKEN, ChatTurnRequest
from src.services.chat_child_image import _collect, _pings_until_done
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.chat_image_selection import select_images
from src.services.chat_llm import render_chat_images

_TEXT_CHUNK_CHARACTERS = 5
_TEXT_CHUNK_INTERVAL_SECONDS = 0.06
_SELECTION_TIMEOUT_SECONDS = 10.0


async def _prepare(
    events: AsyncIterator[dict], req: ChatTurnRequest,
    on_body_completed: Callable[[str], None] | None,
) -> tuple[dict, list[dict]]:
    completed = await _collect(events)
    if completed["event"] == EVENT_ERROR:
        return completed, []
    text = strip_character_image_syntax(completed["ai_output"])
    if on_body_completed is not None:
        on_body_completed(text)
    result = await select_images(
        character_images=req.character_images, history=req.history,
        user_input=req.user_input, ai_output=text, timeout=_SELECTION_TIMEOUT_SECONDS,
    )
    # 기본 이미지가 없으면 요청 목록의 첫 이미지로 대체한다.
    chosen = {}
    for image in req.character_images:
        if image.image_url.strip():
            chosen.setdefault(image.name, image)
            if image.image_name == f"{image.name}_기본":
                chosen[image.name] = image
    chosen.update({image.name: image for image in result.images})
    # 전체 이름을 보존해 기존 별칭 충돌 판정이 바뀌지 않게 한다.
    mappings = [chosen.get(image.name, image) for image in req.character_images]
    rendered, stored, displayed = render_chat_images(text, mappings)
    return {**completed, "ai_output": stored, "character_images": displayed}, rendered


async def stream_with_selected_images(
    events: AsyncIterator[dict], req: ChatTurnRequest, *,
    on_body_completed: Callable[[str], None] | None = None,
) -> AsyncIterator[dict]:
    """본문·Jev를 기다리는 동안 ping, 이후 이미지와 5글자/60ms 대사를 보낸다."""
    preparing = asyncio.create_task(_prepare(events, req, on_body_completed))
    async with aclosing(_pings_until_done(preparing)) as waiting:
        async for ping in waiting:
            yield ping
    completed, rendered = preparing.result()
    if completed["event"] == EVENT_ERROR:
        yield completed
        return
    for event in rendered:
        if event["event"] != EVENT_TOKEN:
            yield event
            continue
        for offset in range(0, len(event["text"]), _TEXT_CHUNK_CHARACTERS):
            await asyncio.sleep(_TEXT_CHUNK_INTERVAL_SECONDS)
            yield {**event, "text": event["text"][offset:offset + _TEXT_CHUNK_CHARACTERS]}
    yield completed
