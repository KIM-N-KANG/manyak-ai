"""실제 SDK를 격리 프로세스에서 검증한다. 공급자 호출·관측 전송은 메모리로 대체한다."""

import subprocess
import sys

import pytest


CHECK = r'''
import asyncio
import json
import logging
import sys
from unittest.mock import patch

from src.core.config import settings
from src.core import tracing

scenario = sys.argv[1]
if scenario == "off":
    settings.manyak_tracing_enabled = False
    tracing.instrument_app(None)
    with tracing.start_span("llm.complete", {"provider": "openai"}):
        assert tracing.log_context() == {}
    from src.core.json_logging import JsonLogFormatter
    record = logging.LogRecord("test", logging.INFO, "", 0, "ok", (), None)
    record.traceId = "spoof"
    record.spanId = "spoof"
    payload = json.loads(JsonLogFormatter().format(record))
    assert "traceId" not in payload and "spanId" not in payload
    assert not any(n.startswith("opentelemetry") for n in sys.modules)
    raise SystemExit(0)

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from starlette.responses import StreamingResponse
from src.core.json_logging import JsonLogFormatter

settings.manyak_tracing_enabled = True
settings.manyak_otlp_traces_endpoint = "http://127.0.0.1:1/v1/traces"
exporter = InMemorySpanExporter()
app = FastAPI()
records = []
lf_client = None
lf_exporter = InMemorySpanExporter()
if scenario == "isolation":
    from langfuse import Langfuse
    from src.core import langfuse as lf
    settings.langfuse_public_key = "pk-lf-infra-isolation"
    settings.langfuse_secret_key = "sk-lf-test"
    settings.langfuse_host = "https://jp.cloud.langfuse.com"
    settings.sentry_environment = "prod"
    providers = []
    def construct(**kwargs):
        providers.append(kwargs["tracer_provider"])
        return Langfuse(**kwargs, span_exporter=lf_exporter)
    with patch("langfuse.Langfuse", side_effect=construct):
        lf.init_langfuse()
    assert lf.is_enabled()
    from langfuse import get_client
    lf_client = get_client()

@app.get("/work")
async def work():
    async def child():
        with tracing.start_span("llm.complete", {
            "provider": "openai", "model": "test-model", "operation": "complete",
            "prompt": "PRIVATE-PROMPT", "url": "https://private/?token=SECRET",
        }):
            record = logging.LogRecord("test", logging.INFO, "", 0, "ok", (), None)
            record.traceId = "spoof"
            record.spanId = "spoof"
            records.append(json.loads(JsonLogFormatter().format(record)))
    if lf_client:
        with lf_client.start_as_current_observation(name="generation", as_type="generation", input="PRIVATE-PROMPT"):
            await asyncio.create_task(child())
    else:
        await asyncio.create_task(child())
    return {"ok": True}

@app.get("/stream/{count}")
async def streaming(count: int):
    async def body():
        with tracing.start_span("llm.stream", {"operation": "stream"}):
            for _ in range(count):
                yield "data: synthetic\n\n"
    return StreamingResponse(body(), media_type="text/event-stream")

@app.get("/fail")
async def fail():
    raise ValueError("PRIVATE-EXCEPTION")

@app.get("/health")
async def health():
    return {"ok": True}

if scenario == "invalid":
    for endpoint in ("", "file:///private", "http:///missing-host", "https://host:bad"):
        settings.manyak_otlp_traces_endpoint = endpoint
        tracing.instrument_app(app)
        assert tracing._state.provider is None
        assert tracing.log_context() == {}
    raise SystemExit(0)

if scenario == "init_failure":
    with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", side_effect=RuntimeError("synthetic")):
        tracing.instrument_app(app)
    assert tracing._state.provider is None
    async def without_tracing():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/work")).status_code == 200
    asyncio.run(without_tracing())
    raise SystemExit(0)

with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=exporter) as factory:
    if scenario == "unavailable":
        # SDK HTTP exporter를 쓰되 전송 계층에서 연결 실패를 재현한다. 외부 네트워크 없음.
        import requests
        class UnavailableSession(requests.Session):
            def post(self, *args, **kwargs):
                raise requests.ConnectionError("synthetic collector unavailable")
        factory.return_value = OTLPSpanExporter(endpoint=settings.manyak_otlp_traces_endpoint, timeout=0.01, session=UnavailableSession())
    tracing.instrument_app(app)
    assert factory.call_args.kwargs["endpoint"] == settings.manyak_otlp_traces_endpoint

if lf_client:
    assert providers[0] is not tracing._state.provider

async def run():
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        parent = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
        assert (await client.get("/work?secret=PRIVATE-QUERY", headers={"traceparent": parent, "tracestate": "vendor=PRIVATE-STATE"})).status_code == 200
        tracing._state.provider.force_flush()
        spans = exporter.get_finished_spans()
        if scenario == "unavailable":
            await tracing.shutdown_tracing()
            return
        root = next(s for s in spans if s.kind == trace.SpanKind.SERVER)
        call = next(s for s in spans if s.name == "llm.complete")
        assert root.context.trace_id == int("1234567890abcdef1234567890abcdef", 16)
        assert root.parent.span_id == int("1234567890abcdef", 16)
        assert call.parent.span_id == root.context.span_id
        assert call.context.trace_id == root.context.trace_id
        assert "PRIVATE" not in str([dict(s.attributes) for s in spans])
        assert "prompt" not in call.attributes and "url" not in call.attributes
        assert b"PRIVATE" not in encode_spans(spans).SerializePartialToString()
        assert records[0]["traceId"] == f"{root.context.trace_id:032x}"
        assert records[0]["spanId"] == f"{root.context.span_id:016x}"
        assert tracing.log_context() == {}
        if lf_client:
            lf_client.flush()
            assert "PRIVATE-PROMPT" in str([dict(s.attributes) for s in lf_exporter.get_finished_spans()])
            assert len(spans) == 2
        exporter.clear()
        assert (await client.get("/work")).status_code == 200
        tracing._state.provider.force_flush()
        roots = [s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
        assert len(roots) == 1 and roots[0].parent is None
        for count in (1, 50):
            exporter.clear()
            assert (await client.get(f"/stream/{count}")).status_code == 200
            tracing._state.provider.force_flush()
            spans = exporter.get_finished_spans()
            assert len(spans) == 2, [(s.name, s.kind) for s in spans]
            root = next(s for s in spans if s.kind == trace.SpanKind.SERVER)
            call = next(s for s in spans if s.name == "llm.stream")
            assert call.parent.span_id == root.context.span_id
            assert call.end_time <= root.end_time
        exporter.clear()
        await client.get("/health")
        await client.get("/work", headers={"traceparent": parent[:-2] + "00"})
        tracing._state.provider.force_flush()
        assert exporter.get_finished_spans() == ()
        assert (await client.get("/fail?token=PRIVATE-QUERY")).status_code == 500
        tracing._state.provider.force_flush()
        for span in exporter.get_finished_spans():
            assert not span.events and not span.links
            assert span.status.description is None
            assert "PRIVATE" not in str(dict(span.attributes))
            assert "PRIVATE" not in span.name
        assert b"PRIVATE" not in encode_spans(exporter.get_finished_spans()).SerializePartialToString()
        await tracing.shutdown_tracing()

asyncio.run(run())
if lf_client:
    lf_client.shutdown()
'''


@pytest.mark.parametrize("scenario", ["off", "invalid", "init_failure", "request", "isolation", "unavailable"])
def test_infra_tracing_sdk(scenario: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHECK, scenario], capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("adapter_name", ["openai_sdk", "google_sdk", "anthropic_sdk"])
@pytest.mark.parametrize("finish", ["consume", "close_other_task", "cancel"])
async def test_adapter_stream_has_one_span_until_consumption_ends(monkeypatch, adapter_name, finish):
    import asyncio
    import importlib
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from opentelemetry.context import Context
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import get_current_span, set_span_in_context

    from src.core import tracing
    from src.services.llm.base import LlmRequest, ResolvedModel, TextDelta

    adapter = importlib.import_module(f"src.services.llm.{adapter_name}")
    exporter = InMemorySpanExporter()
    provider = tracing._create_provider(exporter)
    monkeypatch.setattr(tracing._state, "provider", provider)
    parent = provider.get_tracer("test").start_span("parent")
    token = tracing._request_parent.set(set_span_in_context(parent, Context()))
    began_waiting = asyncio.Event()

    class FakeStream:
        closed = False

        async def __aiter__(self):
            for _ in range(20):
                if adapter_name == "openai_sdk":
                    yield NS(choices=[NS(delta=NS(content="synthetic"))])
                elif adapter_name == "google_sdk":
                    yield NS(text="synthetic")
                else:
                    yield NS(type="content_block_delta", delta=NS(type="text_delta", text="synthetic"))
                if finish == "cancel":
                    began_waiting.set()
                    await asyncio.Event().wait()
            if adapter_name == "anthropic_sdk":
                yield NS(type="message_delta", delta=NS(stop_reason="end_turn"))

        async def close(self):
            self.closed = True

    stream = FakeStream()
    create = AsyncMock(return_value=stream)
    client = NS(chat=NS(completions=NS(create=create)), messages=NS(create=create),
                aio=NS(models=NS(generate_content_stream=create)))
    monkeypatch.setattr(adapter, "_client", lambda _: client)
    if adapter_name == "google_sdk":
        monkeypatch.setattr(adapter, "_build_config", lambda *args: None)
        monkeypatch.setattr(adapter, "_build_contents", lambda *args: [])
    else:
        monkeypatch.setattr(adapter, "_build_kwargs", lambda *args: {})
    req = LlmRequest(model="test-model", messages=[{"role": "user", "content": "PRIVATE-PROMPT"}], max_tokens=16)
    resolved = ResolvedModel(model="test-model", provider="test", adapter="test", use_thinking=False)
    generator = adapter.stream(req, resolved)
    try:
        current_before = get_current_span()
        first = await anext(generator)
        assert isinstance(first, TextDelta)
        assert get_current_span() is current_before
        provider.force_flush()
        assert not exporter.get_finished_spans()  # yield 뒤에도 아직 하나의 호출이 진행 중
        if finish == "consume":
            events = [event async for event in generator]
            assert len([e for e in events if isinstance(e, TextDelta)]) == 19
        elif finish == "close_other_task":
            await asyncio.create_task(generator.aclose())
        else:
            pending = asyncio.create_task(anext(generator))
            await asyncio.wait_for(began_waiting.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "llm.stream"
        assert spans[0].parent.span_id == parent.get_span_context().span_id
        assert "PRIVATE" not in str(dict(spans[0].attributes))
        if finish != "consume":
            assert spans[0].attributes["error.type"] in {"GeneratorExit", "CancelledError"}
        if adapter_name != "google_sdk":
            assert stream.closed
    finally:
        await generator.aclose()
        tracing._request_parent.reset(token)
        parent.end()
        provider.shutdown()


async def test_shutdown_has_a_bound_and_does_not_block_event_loop():
    from threading import Event
    import asyncio

    from src.core.tracing import bounded_shutdown

    release = Event()
    ticks = []

    async def heartbeat():
        await asyncio.sleep(0.01)
        ticks.append(True)

    try:
        await asyncio.wait_for(asyncio.gather(
            bounded_shutdown(lambda: release.wait(5), timeout=0.06), heartbeat(),
        ), timeout=1)
        assert ticks == [True]
    finally:
        release.set()
