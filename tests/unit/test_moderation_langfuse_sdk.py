"""실제 Langfuse·OpenAI SDK로 취소 관측을 검증한다. HTTP·관측 전송은 모두 메모리 안에서 처리한다."""

import subprocess
import sys

import pytest


# langfuse.openai의 전역 계측이 다른 테스트에 남지 않도록 별도 프로세스에서 실행한다.
SDK_CHECK = r'''
import asyncio
import json
import sys
from unittest.mock import patch, AsyncMock

import httpx
import openai
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.api.v1.moderation.story import moderate_story
from src.core import langfuse as lf
from src.core.config import settings
from src.schemas.moderation import StoryModerationRequest
from src.services import llm
from src.services.llm import openai_sdk
from src.services.llm.base import LlmRequest
from src.services.moderation import service
from src.services.moderation.images import ModerationImage, PreparedImages

import langfuse.openai as instrumented

scenario = sys.argv[1]
exporter = InMemorySpanExporter()
client = Langfuse(
    public_key="pk-lf-test-moderation-cancellation", secret_key="sk-lf-test",
    tracer_provider=TracerProvider(), span_exporter=exporter,
)
settings.moderation_model = "gpt-5.6-luna"
settings.moderation_fallback_model = "deepseek-flash"
lf._state.enabled = True
image_uri = "data:image/png;base64,c3ludGhldGljLWltYWdl"
provider_bodies = []

async def run():
    started = asyncio.Event()
    timers = []
    real_timeout = asyncio.timeout

    def controlled_timeout(delay):
        # 짧은 초 수에 기대지 않고 HTTP 요청이 시작된 시점에 해당 timeout을 만료시킨다.
        timer = real_timeout(None)
        timers.append(timer)
        return timer

    async def respond(request):
        body = json.loads(request.content)
        provider_bodies.append(body)
        model = body["model"]
        started.set()
        if scenario in {"all_timeout", "cancel"} or (
            scenario in {"timeout_fallback", "media_fallback"} and model == settings.moderation_model
        ):
            if scenario != "cancel":
                timers[-1].reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"decision":"APPROVED","issues":[]}',
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "prompt_tokens_details": {"cached_tokens": 4}, "prompt_cache_hit_tokens": 4},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        sdk_client = openai.AsyncOpenAI(api_key="test-key", http_client=http)
        with patch("langfuse.get_client", return_value=client), \
             patch.object(instrumented, "get_client", return_value=client), \
             patch.object(openai_sdk, "_client", return_value=sdk_client), \
             patch.object(asyncio, "timeout", side_effect=controlled_timeout), \
             patch.object(client._resources._media_manager, "_process_media") as media_upload, \
             patch.object(service, "fetch_images", new=AsyncMock(return_value=PreparedImages(
                 images=[ModerationImage("thumbnailUrl", "image/png", b"synthetic-image")] if scenario in {"media", "media_fallback"} else [],
                 errors=[],
             ))):
            request = StoryModerationRequest(
                storyId="synthetic-story", title=image_uri if scenario in {"media", "media_fallback"} else "synthetic",
                thumbnailUrl="https://cdn.example.com/synthetic.png" if scenario in {"media", "media_fallback"} else None,
            )
            if scenario == "cancel":
                task = asyncio.create_task(moderate_story(request))
                await asyncio.wait_for(started.wait(), timeout=10)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("취소가 검수 결과로 바뀜")
            else:
                response = await moderate_story(request)
                assert response.decision == ("REJECTED" if scenario == "all_timeout" else "APPROVED")
            if scenario in {"media", "media_fallback"}:
                assert image_uri in json.dumps(provider_bodies[0])
                assert "metadata" not in provider_bodies[0]
            if scenario == "media":
                await llm.complete(LlmRequest(model="gpt-5.6-luna", messages=[{"role": "user", "content": "ordinary call"}]))
            media_upload.assert_not_called()

asyncio.run(run())
client.flush()
spans = exporter.get_finished_spans()
roots = [s for s in spans if s.name == "게시물 검수"]
calls = sorted((s for s in spans if s.name == "검수 모델 호출"), key=lambda s: s.start_time)
assert len(roots) == 1
assert len(calls) == (2 if scenario in {"all_timeout", "timeout_fallback", "media_fallback"} else 1)
for span in calls:
    assert span.parent.span_id == roots[0].context.span_id
    assert span.context.trace_id == roots[0].context.trace_id
    assert span.end_time is not None and span.end_time >= span.start_time
    assert span.attributes["langfuse.observation.type"] == "generation"
if scenario not in {"success", "media"}:
    assert "langfuse.observation.model.name" not in calls[0].attributes
    assert calls[0].attributes["langfuse.observation.metadata.model"] == settings.moderation_model
    assert calls[0].attributes["langfuse.observation.level"] == "ERROR"
    assert calls[0].attributes["langfuse.observation.status_message"] == (
        "CancelledError" if scenario == "cancel" else "TimeoutError"
    )
if scenario == "all_timeout":
    assert "langfuse.observation.model.name" not in calls[1].attributes
    assert calls[1].attributes["langfuse.observation.status_message"] == "TimeoutError"
if scenario in {"success", "timeout_fallback", "media", "media_fallback"}:
    successful = calls[-1]
    assert successful.attributes["langfuse.observation.model.name"] in {settings.moderation_model, settings.moderation_fallback_model}
    assert json.loads(successful.attributes["langfuse.observation.output"])["decision"] == "APPROVED"
    usage = json.loads(successful.attributes["langfuse.observation.usage_details"])
    assert usage["input"] == 6 and usage["input_cached_tokens"] == 4 and usage["output"] == 5
    if scenario in {"timeout_fallback", "media_fallback"}:
        assert successful.attributes["langfuse.observation.metadata.pricing_window"] in {"peak", "off_peak"}
generations = [s for s in spans if s.attributes.get("langfuse.observation.type") == "generation"]
assert len(generations) == len(calls) + (1 if scenario == "media" else 0)
if scenario == "media":
    assert len([s for s in spans if s.name == "OpenAI-generation"]) == 1
if scenario in {"media", "media_fallback"}:
    recorded = str([dict(s.attributes) for s in spans])
    assert image_uri not in recorded
    assert "c3ludGhldGljLWltYWdl" not in recorded
    call_input = json.loads(calls[0].attributes["langfuse.observation.input"])
    assert call_input["images"] == [{"path": "thumbnailUrl", "content_type": "image/png", "size_bytes": 15}]
client.shutdown()
print("SDK cancellation observation verified:", scenario)
'''


@pytest.mark.parametrize("scenario", ["timeout_fallback", "all_timeout", "cancel", "success", "media", "media_fallback"])
def test_real_sdk_records_finished_model_calls(scenario):
    result = subprocess.run(
        [sys.executable, "-c", SDK_CHECK, scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
