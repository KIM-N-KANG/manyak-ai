"""요약 서비스의 재시도, 취소, 사용량과 실제 SDK 경계를 검증한다."""

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from src.core import langfuse
from src.schemas.chat_compact import ChatCompactRequest
from src.services import chat_compact as service
from src.services.llm import anthropic_sdk, openai_sdk
from src.services.llm.base import LlmConfigError, LlmResult, LlmUnavailable, TokenUsage


def request() -> ChatCompactRequest:
    return ChatCompactRequest(summary="old", history=[{"role": "USER", "content": "new"}])


def result(text: str = "summary", model: str = "claude-sonnet-5-5") -> LlmResult:
    return LlmResult(text=text, model=model, provider="anthropic", usage=TokenUsage(100, 20, 10, 30))


@pytest.fixture
def observations(monkeypatch):
    records = []

    @contextmanager
    def observe(*args, **kwargs):
        record = dict(kwargs)
        record["metadata"] = dict(kwargs.get("metadata", {}))
        records.append(record)
        yield SimpleNamespace(
            set_metadata=lambda **values: record["metadata"].update(values),
            set_usage=lambda **values: record.update(values),
            set_output=lambda value: record.update(output=value),
            mark_failed=lambda exc: record.update(error=type(exc).__name__),
        )

    monkeypatch.setattr(service, "observe_model_call", observe)
    return records


@pytest.mark.parametrize("success_at", [1, 2, 3, 4, None])
async def test_attempt_order_settings_and_observation(monkeypatch, observations, success_at):
    requests = []

    async def complete(req):
        requests.append(req)
        if len(requests) == success_at:
            return result("가" * 3000, req.model)
        # 빈 응답도 사용량을 보존하고 재시도한다.
        return result(" \n", req.model)

    monkeypatch.setattr(service.llm, "complete", complete)
    progress = service.CompactProgress()
    if success_at is None:
        with pytest.raises(service.CompactFailed):
            await service.generate_compact(request(), progress)
    else:
        assert await service.generate_compact(request(), progress) == "가" * 3000
    count = success_at or 4
    assert [req.model for req in requests] == [*([service.MODELS[0]] * 2), *([service.MODELS[1]] * 2)][:count]
    assert progress.attempts == count
    assert progress.fallback_used == (count > 2)
    for req in requests:
        assert (req.reasoning_effort, req.max_tokens, req.timeout, req.max_retries) == ("low", 12288, 60.0, 0)
        assert req.automatic_observation is False
        assert req.messages == requests[0].messages
    assert [item["metadata"]["status"] for item in observations] == ["failed"] * (count - bool(success_at)) + (["success"] if success_at else [])
    assert all(item["usage_details"] == {"input": 60, "output": 20, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 30} for item in observations)


@pytest.mark.parametrize("exception", [LlmConfigError("config"), RuntimeError("code")])
async def test_internal_error_not_retried(monkeypatch, exception):
    calls = []

    async def complete(req):
        calls.append(req)
        raise exception

    monkeypatch.setattr(service.llm, "complete", complete)
    with pytest.raises(type(exception)):
        await service.generate_compact(request(), service.CompactProgress())
    assert len(calls) == 1


async def test_timeout_stops_each_attempt(monkeypatch):
    monkeypatch.setattr(service, "CALL_TIMEOUT", 0.01)
    stopped = []

    async def complete(req):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(req.model)

    monkeypatch.setattr(service.llm, "complete", complete)
    with pytest.raises(service.CompactFailed):
        await service.generate_compact(request(), service.CompactProgress())
    assert stopped == [service.MODELS[0]] * 2 + [service.MODELS[1]] * 2


@pytest.mark.parametrize("cancel_at", [1, 3])
async def test_cancellation_stops_following_attempts(monkeypatch, observations, cancel_at):
    started = asyncio.Event()
    stopped = asyncio.Event()
    calls = []

    async def complete(req):
        calls.append(req)
        if len(calls) < cancel_at:
            return result("")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(service.llm, "complete", complete)
    task = asyncio.create_task(service.generate_compact(request(), service.CompactProgress()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert len(calls) == cancel_at
    assert observations[-1]["metadata"]["status"] == "cancelled"


async def test_provider_error_original_not_logged_or_sent(monkeypatch, caplog):
    captures = []

    async def complete(req):
        raise LlmUnavailable("PRIVATE_SOURCE_TEXT", model=req.model, provider="anthropic")

    monkeypatch.setattr(service.llm, "complete", complete)
    monkeypatch.setattr(service, "capture_ai_exception", lambda exc, **kw: captures.append((exc, kw)))
    with pytest.raises(service.CompactFailed):
        await service.generate_compact(request(), service.CompactProgress())
    assert len(captures) == 4
    assert "PRIVATE_SOURCE_TEXT" not in caplog.text
    for exc, metadata in captures:
        assert "PRIVATE_SOURCE_TEXT" not in str(exc)
        assert exc.__context__ is None and exc.__cause__ is None
        assert metadata["error_code"] == "provider_unavailable"


@pytest.mark.parametrize("succeed", [False, True])
async def test_real_sdks_have_no_hidden_retries(monkeypatch, succeed):
    bodies = []

    def respond(req):
        body = json.loads(req.content)
        bodies.append(body)
        if succeed and len(bodies) == 4:
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            })
        return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "test"}})

    async with AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as anthropic, AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as openai:
        monkeypatch.setattr(anthropic_sdk, "_client", lambda provider: anthropic)
        monkeypatch.setattr(openai_sdk, "_client", lambda provider: openai)
        if succeed:
            assert await service.generate_compact(request(), service.CompactProgress()) == "done"
        else:
            with pytest.raises(service.CompactFailed):
                await service.generate_compact(request(), service.CompactProgress())
    assert [body["model"] for body in bodies] == [service.MODELS[0]] * 2 + [service.MODELS[1]] * 2
    assert bodies[0]["output_config"]["effort"] == "low"
    assert bodies[2]["reasoning_effort"] == "low"


@pytest.mark.parametrize("success_at", [1, 2, 3, 4, None])
async def test_length_limit_retries_without_truncation(monkeypatch, observations, success_at):
    calls = []
    valid = "가" * 2998 + " \n"
    oversized = valid + " "

    async def complete(req):
        calls.append(req)
        return result(valid if len(calls) == success_at else oversized, req.model)

    monkeypatch.setattr(service.llm, "complete", complete)
    progress = service.CompactProgress()
    if success_at is None:
        with pytest.raises(service.CompactFailed):
            await service.generate_compact(request(), progress)
    else:
        assert await service.generate_compact(request(), progress) == valid
    assert len(calls) == (success_at or 4)
    assert [req.model for req in calls] == ([service.MODELS[0]] * 2 + [service.MODELS[1]] * 2)[:len(calls)]
    for record in observations[:(success_at - 1 if success_at else 4)]:
        assert record["metadata"]["error_code"] == "invalid_ai_response"
        assert record["metadata"]["status"] == "failed"
        assert record["usage_details"]["output"] == 20


async def test_observation_failure_does_not_stop_generation(monkeypatch):
    import langfuse as sdk

    def fail():
        raise RuntimeError("collector unavailable")

    async def complete(req):
        return result()

    monkeypatch.setattr(langfuse._state, "enabled", True)
    monkeypatch.setattr(sdk, "get_client", fail)
    monkeypatch.setattr(service.llm, "complete", complete)
    assert await service.generate_compact(request(), service.CompactProgress()) == "summary"
