"""검수에 필요한 공통 호출 옵션과 실제 SDK 전송 형식을 가짜 HTTP로 검증한다."""

import json

import httpx
import pytest
from openai import AsyncOpenAI

from src.core.config import Settings
from src.services import llm
from src.services.llm import openai_sdk, registry
from src.services.llm.base import LlmConfigError, LlmRequest, LlmUnavailable, image_part, text_part
from src.services.moderation.models import ModelDecision


def request(model="gpt-5.6-luna"):
    return LlmRequest(
        model=model, timeout=60, max_retries=0,
        messages=[{"role": "user", "content": [text_part("JSON 검수"), image_part(data=b"abc", content_type="image/png")]}],
        response_schema=ModelDecision.model_json_schema() if model == "gpt-5.6-luna" else None,
        json_mode=model == "deepseek-flash", reasoning_effort="high" if model == "gpt-5.6-luna" else None,
    )


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "deepseek-flash"])
async def test_preflight_body_matches_sdk_body_without_network(monkeypatch, model):
    captured = []

    def handler(req):
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"decision":"APPROVED","issues":[]}'}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AsyncOpenAI(api_key="test", http_client=http_client)
        monkeypatch.setattr(openai_sdk, "_client", lambda provider: client)
        req = request(model)
        body = llm.request_body(req)
        assert captured == []
        await llm.complete(req)
        assert captured == [body]
        assert "timeout" not in body and "max_retries" not in body and "metadata" not in body
        if model == "gpt-5.6-luna":
            assert body["reasoning_effort"] == "high"
            assert body["response_format"]["json_schema"]["strict"] is True
            assert registry.resolve(model).reasoning_effort == "none"
        else:
            assert body["response_format"] == {"type": "json_object"}
            assert body["thinking"] == {"type": "disabled"}


async def test_moderation_disables_sdk_retries_without_changing_cached_client(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(500, json={"error": {"message": "temporary failure"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AsyncOpenAI(api_key="test", http_client=http_client, max_retries=2)
        monkeypatch.setattr(openai_sdk, "_client", lambda provider: client)
        with pytest.raises(LlmUnavailable):
            await llm.complete(request())
        assert len(calls) == 1
        assert client.max_retries == 2


@pytest.mark.parametrize("overrides", [
    {"moderation_model": "unknown"}, {"moderation_model": "deepseek-flash"},
    {"moderation_model": "gpt-5.6-terra"}, {"moderation_fallback_model": "gpt-5.6-luna"},
])
def test_startup_rejects_invalid_moderation_models(monkeypatch, overrides):
    config = Settings(_env_file=None, deepseek_api_key="test", openai_api_key="test", **overrides)
    monkeypatch.setattr(registry, "settings", config)
    with pytest.raises(LlmConfigError, match="MODERATION"):
        llm.validate_startup()


def test_conflicting_output_modes_are_rejected():
    with pytest.raises(LlmConfigError):
        llm.request_body(LlmRequest(model="gpt-5.6-luna", messages=[], json_mode=True, response_schema={}))
