"""compact JSON 계약, 연결 종료와 요청별 격리."""

import asyncio
import json

import httpx
import pytest

from src.api.v1 import chat
from src.main import app
from src.services.chat_compact import CompactFailed
from src.services import chat_compact as compact_service
from src.services.llm.base import LlmResult, TokenUsage


async def test_all_summaries_too_long_returns_502(client, monkeypatch):
    calls = []

    async def complete(req):
        calls.append(req.model)
        return LlmResult(text="가" * 3001, model=req.model, provider="anthropic", usage=TokenUsage())

    monkeypatch.setattr(compact_service.llm, "complete", complete)
    response = await client.post("/api/v1/chat/compact", json={"summary": "기존 요약", "history": []})
    assert response.status_code == 502
    assert response.json() == {"code": "COMPACT_FAILED", "message": "대화 요약을 생성하지 못했습니다."}
    assert calls == [compact_service.MODELS[0]] * 2 + [compact_service.MODELS[1]] * 2


@pytest.mark.parametrize("summary", ["short", "가" * 3000])
async def test_success(client, monkeypatch, summary):
    async def generate(payload, progress):
        assert payload.summary == "old"
        assert payload.history[-1].content == "current"
        return summary

    monkeypatch.setattr(chat, "generate_compact", generate)
    response = await client.post("/api/v1/chat/compact", json={
        "summary": "old", "history": [{"role": "ASSISTANT", "content": "current"}],
    })
    assert response.status_code == 200
    assert response.json() == {"summary": summary}


@pytest.mark.parametrize("body", [{}, {"summary": "PRIVATE"}, {"summary": "PRIVATE", "history": [{"role": "SYSTEM", "content": "PRIVATE"}]}])
async def test_invalid_request_sanitized(client, monkeypatch, body):
    async def unexpected(*args):
        pytest.fail("invalid request invoked generation")

    monkeypatch.setattr(chat, "generate_compact", unexpected)
    response = await client.post("/api/v1/chat/compact", json=body)
    assert response.status_code == 422
    assert "PRIVATE" not in response.text
    assert all(set(error) <= {"type", "loc", "msg"} for error in response.json()["detail"])


@pytest.mark.parametrize("body", [
    {"summary": "", "history": []},
    {"summary": " \n\t", "history": [{"role": "USER", "content": "　 "}]},
    {"summary": "[[https://example.invalid/private]]", "history": []},
    {"summary": "", "history": [
        {"role": "USER", "content": " \n"},
        {"role": "ASSISTANT", "content": "[[https://example.invalid/private]]\n\n"},
    ]},
])
async def test_empty_content_rejected_without_generation(client, monkeypatch, body):
    async def unexpected(*args):
        pytest.fail("empty input invoked generation")

    monkeypatch.setattr(chat, "generate_compact", unexpected)
    response = await client.post("/api/v1/chat/compact", json=body)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert "요약할 대화 내용이 없습니다." in errors[0]["msg"]
    assert all(set(error) <= {"type", "loc", "msg"} for error in errors)
    assert "example.invalid" not in response.text


@pytest.mark.parametrize("body", [
    {"summary": "기존 요약", "history": []},
    {"summary": " \n", "history": [{"role": "USER", "content": "새 대화"}]},
    {"summary": "[[url]]", "history": [{"role": "ASSISTANT", "content": "[[url]]\n본문"}]},
])
async def test_one_content_source_is_enough(client, monkeypatch, body):
    async def generate(payload, progress):
        assert payload.model_dump() == body
        return "요약 결과"

    monkeypatch.setattr(chat, "generate_compact", generate)
    response = await client.post("/api/v1/chat/compact", json=body)
    assert response.status_code == 200
    assert response.json() == {"summary": "요약 결과"}


@pytest.mark.parametrize("exception, status", [(CompactFailed("PRIVATE"), 502), (RuntimeError("PRIVATE"), 500)])
async def test_failure_contract(monkeypatch, exception, status):
    async def generate(*args):
        raise exception

    monkeypatch.setattr(chat, "generate_compact", generate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/api/v1/chat/compact", json={"summary": "기존 요약", "history": []})
    assert response.status_code == status
    assert "PRIVATE" not in response.text
    if status == 502:
        assert response.json() == {"code": "COMPACT_FAILED", "message": "대화 요약을 생성하지 못했습니다."}


async def test_disconnect_cancels_only_its_request(client, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    messages = []

    async def generate(payload, progress):
        if payload.summary == "other":
            return "other summary"
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(chat, "generate_compact", generate)
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": json.dumps({"summary": "cancel", "history": []}).encode(), "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/v1/chat/compact", "raw_path": b"/api/v1/chat/compact",
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "server": ("test", 80), "client": ("test", 1234), "root_path": "",
    }
    task = asyncio.create_task(app(scope, receive, send))
    response = await client.post("/api/v1/chat/compact", json={"summary": "other", "history": []})
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    assert messages == []
    assert response.json() == {"summary": "other summary"}
