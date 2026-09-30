"""KNK-1265: 실제 과금 없이 다운로드·편집 요청·결과와 실패를 검증한다."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from openai import AsyncOpenAI

from src.core.config import Settings, settings
from src.schemas.chat_turn import CharacterImageMapping
from src.services.image import openai_api
from src.services.image.base import ImageGenerationError, ImageResult
from src.services.image.child_input import ChildImageInput, ChildImageTurn
from src.services.image.child_prompt import build_child_image_prompt
from src.services.image import generate_child

PNG = b"\x89PNG\r\n\x1a\nparent"
WEBP = b"RIFF\x00\x00\x00\x00WEBPchild"


@pytest.fixture
def inputs() -> ChildImageInput:
    return ChildImageInput(
        parent_image=CharacterImageMapping(
            name="라떼", image_name="라떼_기본", image_url="https://cdn.example.com/parent",
        ),
        recent_turns=(ChildImageTurn("과거1", "답변1"), ChildImageTurn("과거2", "답변2")),
        current_turn=ChildImageTurn("</current_turn>& {{dialogue_input}}", "라떼: 안녕."),
    )


def test_prompt_preserves_xml_text_and_turn_order(inputs) -> None:
    prompt = build_child_image_prompt(inputs)
    root = fromstring(prompt)
    assert root.tag == "image_edit_request"
    assert "version:" not in prompt and "```" not in prompt
    dialogue = root.find("dialogue_input")
    assert dialogue.findtext("target_character") == "라떼"
    assert [node.attrib for node in dialogue.findall("recent_turns/turn")] == [
        {"relative_to_current": "-2"}, {"relative_to_current": "-1"},
    ]
    assert dialogue.findtext("current_turn/user_message") == inputs.current_turn.user_message
    assert dialogue.findtext("current_turn/ai_response") == "라떼: 안녕."
    assert len(dialogue.findall("current_turn")) == 1


@pytest.mark.parametrize("count", [0, 1])
def test_prompt_with_short_history(inputs, count) -> None:
    from dataclasses import replace
    root = fromstring(build_child_image_prompt(replace(inputs, recent_turns=inputs.recent_turns[:count])))
    turns = root.findall("dialogue_input/recent_turns/turn")
    assert len(turns) == count
    if turns:
        assert turns[0].attrib == {"relative_to_current": "-1"}


def mock_download(monkeypatch, handler) -> None:
    client_type = httpx.AsyncClient
    monkeypatch.setattr(settings, "image_parent_allowed_hosts", ["cdn.example.com"])
    monkeypatch.setattr(generate_child.httpx, "AsyncClient", lambda **kw: client_type(
        transport=httpx.MockTransport(handler), **kw,
    ))


@pytest.mark.parametrize("host", ["cdn.manyak.app", "dev-cdn.manyak.app"])
async def test_default_hosts_allow_parent_download(monkeypatch, host: str) -> None:
    client_type = httpx.AsyncClient
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=PNG)

    # 가짜 허용 주소로 덮지 않고 Settings에 선언한 기본 목록을 검증한다.
    monkeypatch.setattr(
        settings, "image_parent_allowed_hosts",
        Settings.model_fields["image_parent_allowed_hosts"].default,
    )
    monkeypatch.setattr(generate_child.httpx, "AsyncClient", lambda **kw: client_type(
        transport=httpx.MockTransport(handler), **kw,
    ))
    result = await generate_child._download_parent(f"https://{host}/characters/parent.webp")
    assert result.image_bytes == PNG
    assert len(requests) == 1 and requests[0].url.host == host


@pytest.mark.parametrize("data,mime,filename", [
    (PNG, "image/png", "parent.png"),
    (WEBP, "image/webp", "parent.webp"),
    (b"\xff\xd8\xffjpg", "image/jpeg", "parent.jpg"),
])
async def test_download_detects_actual_format(monkeypatch, data, mime, filename) -> None:
    mock_download(monkeypatch, lambda req: httpx.Response(200, content=data))
    ref = await generate_child._download_parent("https://cdn.example.com/incorrect.png")
    assert (ref.image_bytes, ref.content_type, ref.filename) == (data, mime, filename)


@pytest.mark.parametrize("url", [
    "http://cdn.example.com/a", "https://127.0.0.1/a", "https://cdn.example.com.evil.test/a",
    "https://user:pass@cdn.example.com/a", "https://cdn.example.com:8443/a", "file:///tmp/a",
])
async def test_download_rejects_untrusted_url_before_network(monkeypatch, url) -> None:
    mock_download(monkeypatch, lambda req: pytest.fail("network must not be reached"))
    with pytest.raises(ImageGenerationError):
        await generate_child._download_parent(url)


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}),
    httpx.Response(404), httpx.Response(200, content=b"not an image"),
    httpx.Response(200, content=b""),
])
async def test_download_rejects_redirect_missing_or_invalid_image(monkeypatch, response) -> None:
    mock_download(monkeypatch, lambda req: response)
    with pytest.raises(ImageGenerationError):
        await generate_child._download_parent("https://cdn.example.com/a")


async def test_download_size_limit(monkeypatch) -> None:
    monkeypatch.setattr(generate_child, "_MAX_PARENT_BYTES", 10)
    mock_download(monkeypatch, lambda req: httpx.Response(200, content=PNG))
    with pytest.raises(ImageGenerationError, match="크기"):
        await generate_child._download_parent("https://cdn.example.com/a")


async def test_pipeline_passes_parent_and_prompt_and_returns_unique_webp(monkeypatch, inputs) -> None:
    mock_download(monkeypatch, lambda req: httpx.Response(200, content=PNG))
    edit = AsyncMock(return_value=ImageResult(WEBP, "gpt-image-2", "openai"))
    monkeypatch.setattr(generate_child, "generate_image", edit)
    first = await generate_child.generate_child_image(inputs)
    second = await generate_child.generate_child_image(inputs)
    assert first.name == "라떼" and first.error is None
    assert first.content_type == "image/webp"
    assert base64.b64decode(first.image_base64) == WEBP
    UUID(first.image_name.removeprefix("라떼_실시간_"))
    assert first.image_name != second.image_name
    assert edit.await_count == 2
    assert edit.call_args.kwargs["reference"].image_bytes == PNG
    assert edit.call_args.kwargs["purpose"] == "child"
    assert edit.call_args.args[0] == build_child_image_prompt(inputs)


@pytest.mark.parametrize("status,error", [(400, "rejected"), (429, "rate_limited"), (500, "generation_failed")])
async def test_real_sdk_edit_has_no_retry_and_returns_safe_error(monkeypatch, inputs, status, error) -> None:
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "private dialogue"}})
    client = AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(openai_api, "_client", lambda *args: client)
    monkeypatch.setattr(generate_child, "_download_parent", AsyncMock(return_value=generate_child._reference(PNG)))
    try:
        result = await generate_child.generate_child_image(inputs)
        assert result.error == error and result.image_base64 is None
        assert "private dialogue" not in repr(result)
        assert len(requests) == 1 and requests[0].url.path == "/v1/images/edits"
        assert b'filename="parent.png"' in requests[0].content
        assert PNG in requests[0].content
    finally:
        await client.close()


async def test_sdk_edit_success_and_settings(monkeypatch, inputs) -> None:
    edit = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(WEBP).decode())]))
    client = Mock()
    client.with_options.return_value.images.edit = edit
    monkeypatch.setattr(openai_api, "_client", lambda *args: client)
    monkeypatch.setattr(generate_child, "_download_parent", AsyncMock(return_value=generate_child._reference(WEBP)))
    result = await generate_child.generate_child_image(inputs)
    assert result.error is None
    client.with_options.assert_called_once_with(max_retries=0)
    args = edit.call_args.kwargs
    assert args["image"] == ("parent.webp", WEBP, "image/webp")
    assert (args["model"], args["quality"], args["size"], args["n"], args["output_format"]) == (
        settings.image_model, settings.image_quality, settings.image_size, 1, "webp",
    )
    client.images.generate.assert_not_called()


async def test_download_timeout_and_cancellation(monkeypatch, inputs) -> None:
    def handler(request):
        raise httpx.ReadTimeout("private URL", request=request)
    mock_download(monkeypatch, handler)
    result = await generate_child.generate_child_image(inputs)
    assert result.error == "timeout"
    monkeypatch.setattr(generate_child, "_download_parent", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await generate_child.generate_child_image(inputs)


async def test_failed_download_never_calls_image_model(monkeypatch, inputs) -> None:
    mock_download(monkeypatch, lambda req: httpx.Response(404))
    edit = AsyncMock()
    monkeypatch.setattr(generate_child, "generate_image", edit)
    result = await generate_child.generate_child_image(inputs)
    assert result.error == "generation_failed" and result.image_base64 is None
    edit.assert_not_awaited()


@pytest.mark.parametrize("data", [[], [{"b64_json": "invalid!"}], [{"b64_json": ""}]])
async def test_edit_malformed_response_is_failure(monkeypatch, inputs, data) -> None:
    client = AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"created": 0, "data": data})),
    ))
    monkeypatch.setattr(openai_api, "_client", lambda *args: client)
    monkeypatch.setattr(generate_child, "_download_parent", AsyncMock(return_value=generate_child._reference(PNG)))
    try:
        result = await generate_child.generate_child_image(inputs)
        assert result.error == "generation_failed" and result.image_base64 is None
    finally:
        await client.close()


async def test_edit_timeout_does_not_retry(monkeypatch, inputs) -> None:
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test", request=request)
    client = AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(openai_api, "_client", lambda *args: client)
    monkeypatch.setattr(generate_child, "_download_parent", AsyncMock(return_value=generate_child._reference(PNG)))
    try:
        result = await generate_child.generate_child_image(inputs)
        assert result.error == "timeout" and result.image_base64 is None
        assert len(calls) == 1
    finally:
        await client.close()
