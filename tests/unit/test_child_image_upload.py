import asyncio
import base64
import logging
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from src.core.config import settings
from src.schemas.chat_turn import ChatImageSlot
from src.services import chat_child_image as service
from src.services.image import upload_child
from src.services.image.generate_child import ChildImageResult


@pytest.fixture
def slot(monkeypatch):
    monkeypatch.setattr(settings, "image_upload_allowed_hosts", ["bucket.s3.amazonaws.com"])
    return ChatImageSlot(key="test.webp", upload_url="https://bucket.s3.amazonaws.com/test.webp?signature=private",
                         public_url="https://cdn.manyak.app/test.webp")


@pytest.fixture
def child():
    return ChildImageResult("name", "child", base64.b64encode(b"image-bytes").decode())


@pytest.mark.parametrize("status", [200, 204, 302, 403, 500])
async def test_put_result_and_no_signed_url_log(monkeypatch, slot, child, caplog, status):
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://other.example/image"})

    monkeypatch.setattr(upload_child.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(send))
    with caplog.at_level(logging.DEBUG):
        result = await upload_child.upload_child_image(child, slot)
    assert len(requests) == 1
    assert requests[0].method == "PUT"
    assert str(requests[0].url) == slot.upload_url
    assert requests[0].content == b"image-bytes"
    assert requests[0].headers["content-type"] == "image/webp"
    assert "signature=private" not in caplog.text
    if status < 300:
        assert result.image_url == slot.public_url and result.error is None
    else:
        assert result.error == "generation_failed" and result.image_base64 is None


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.WriteTimeout])
async def test_upload_network_failure(monkeypatch, slot, child, error):
    def send(request):
        raise error(slot.upload_url)
    monkeypatch.setattr(upload_child.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(send))
    result = await upload_child.upload_child_image(child, slot)
    assert result.error == ("timeout" if error is httpx.WriteTimeout else "generation_failed")
    assert result.image_base64 is None and result.image_url is None


async def test_disallowed_host_never_connects(monkeypatch, slot, child):
    monkeypatch.setattr(settings, "image_upload_allowed_hosts", [])
    def forbidden(**kwargs):
        pytest.fail("허용되지 않은 호스트로 연결함")
    monkeypatch.setattr(upload_child.httpx, "AsyncHTTPTransport", forbidden)
    assert (await upload_child.upload_child_image(child, slot)).error == "generation_failed"


async def test_generation_failure_skips_upload(monkeypatch, slot):
    def forbidden(**kwargs):
        pytest.fail("생성 실패 후 업로드함")
    monkeypatch.setattr(upload_child.httpx, "AsyncHTTPTransport", forbidden)
    child = ChildImageResult("name", "child", error="rejected")
    assert await upload_child.upload_child_image(child, slot) is child


@pytest.mark.parametrize("cancel", [False, True])
async def test_upload_obeys_shared_deadline_and_cancellation(monkeypatch, slot, child, cancel):
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def send(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    monkeypatch.setattr(upload_child.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(send))
    monkeypatch.setattr(service, "generate_child_image", AsyncMock(return_value=child))
    observation = service.ChildImageObservation()
    task = asyncio.create_task(service._generate_before_deadline(None, time.monotonic() + (5 if cancel else .05), observation, slot))
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert cleaned.is_set()
    assert observation.reason == ("cancelled" if cancel else "timeout")
