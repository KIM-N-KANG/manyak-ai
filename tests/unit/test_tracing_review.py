"""KNK-1554 F1/F2: SDK 전역 상태는 프로세스로 격리하고 최종 export를 검사한다."""

import os
import subprocess
import sys

import pytest


SAMPLING = r'''
import asyncio
import sys
from unittest.mock import patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from src.core import langfuse as lf, tracing
from src.core.config import settings

settings.langfuse_public_key = "pk-lf-sampling-review"
settings.langfuse_secret_key = "sk-test"
settings.langfuse_host = "https://jp.cloud.langfuse.com"
settings.sentry_environment = "prod"
settings.manyak_tracing_enabled = True
settings.manyak_otlp_traces_endpoint = "http://unused/v1/traces"
lf_exporter, infra_exporter = InMemorySpanExporter(), InMemorySpanExporter()
clients = []
def construct(**kwargs):
    client = Langfuse(**kwargs, span_exporter=lf_exporter)
    clients.append(client)
    return client
with patch("langfuse.Langfuse", side_effect=construct):
    lf.init_langfuse()
assert lf.is_enabled()
client = clients[0]
app = FastAPI()
@app.get("/work")
async def work():
    with lf.observe_request("request", input_data="PRIVATE-INPUT"):
        with lf.observe_generation("generation", model="test-model", input_data="PRIVATE-PROMPT") as generation:
            generation.finish(output="PRIVATE-OUTPUT")
            with tracing.start_span("llm.complete", {"operation": "complete"}):
                return {"ok": True}
with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=infra_exporter):
    tracing.instrument_app(app)

async def run():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        # RatioBased는 하위 64bit로 결정한다. 확률에 기대지 않고 채택·제외 양쪽을 검사한다.
        for trace_id in ("00000000000000010000000000000001", "ffffffffffffffffffffffffffffffff"):
            lf_exporter.clear()
            infra_exporter.clear()
            header = f"00-{trace_id}-1234567890abcdef-{sys.argv[1]}"
            with patch("langfuse.get_client", return_value=client):
                assert (await http.get("/work", headers={"traceparent": header})).status_code == 200
            client.flush()
            tracing._state.provider.force_flush()
            assert len(infra_exporter.get_finished_spans()) == (0 if sys.argv[1] == "00" else 2)
            spans = lf_exporter.get_finished_spans()
            rate = float(settings.langfuse_sample_rate)
            expected = rate == 1 or (rate == 0.5 and trace_id.startswith("0000"))
            assert {s.name for s in spans} == ({"request", "generation"} if expected else set())
            if expected:
                request = next(s for s in spans if s.name == "request")
                generation = next(s for s in spans if s.name == "generation")
                assert request.context.trace_id == int(trace_id, 16)
                assert generation.parent.span_id == request.context.span_id
                assert "PRIVATE-PROMPT" in str(dict(generation.attributes))
    await tracing.shutdown_tracing()
asyncio.run(run())
client.shutdown()
'''


ROUTES = r'''
import asyncio
from unittest.mock import patch
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.routing import Host
from starlette.responses import JSONResponse
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from src.core import tracing
from src.core.config import settings

settings.manyak_tracing_enabled = True
settings.manyak_otlp_traces_endpoint = "http://unused/v1/traces"
exporter = InMemorySpanExporter()
app = FastAPI()
router = APIRouter()
@router.get("/items/{item_id}")
async def item(item_id: str):
    return {"ok": True}
@router.post("/compile")
async def compile_story():
    return {"ok": True}
app.include_router(router, prefix="/api")
async def fallback(scope, receive, send):
    await JSONResponse({"ok": True})(scope, receive, send)
app.router.routes.append(Host("fallback.test", app=fallback))
with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=exporter):
    tracing.instrument_app(app)

async def run():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path, host, template in (
            ("GET", "/api/items/PRIVATE-ID?token=PRIVATE-QUERY", "test", "/api/items/{item_id}"),
            ("GET", "/api/items/PRIVATE-OTHER", "test", "/api/items/{item_id}"),
            ("POST", "/api/compile?token=PRIVATE-QUERY", "test", "/api/compile"),
            ("GET", "/PRIVATE-UNMATCHED?token=PRIVATE-QUERY", "test", None),
            ("GET", "/PRIVATE-FALLBACK?token=PRIVATE-QUERY", "fallback.test", None),
        ):
            exporter.clear()
            response = await client.request(method, path, headers={"host": host})
            assert response.status_code == (404 if "UNMATCHED" in path else 200)
            tracing._state.provider.force_flush()
            spans = exporter.get_finished_spans()
            assert len(spans) == 1
            span = spans[0]
            assert span.name == (f"{method} {template}" if template else "HTTP request")
            assert span.attributes.get("http.route") == template
            # 메모리 속성뿐 아니라 collector로 전달할 protobuf 전체를 검사한다.
            encoded = encode_spans(spans).SerializePartialToString()
            assert b"PRIVATE" not in encoded
            assert b"manyak.route_template" not in encoded
            assert b"http.url" not in encoded and b"url.full" not in encoded
    await tracing.shutdown_tracing()
asyncio.run(run())
'''


def _run(code: str, *args: str, rate: str | None = None) -> None:
    env = dict(os.environ)
    env.pop("LANGFUSE_SAMPLE_RATE", None)
    if rate is not None:
        env["LANGFUSE_SAMPLE_RATE"] = rate
    result = subprocess.run(
        [sys.executable, "-c", code, *args], env=env,
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("rate", [None, "1", "0", "0.5"])
@pytest.mark.parametrize("parent_flags", ["00", "01"])
def test_langfuse_sampling_is_independent_of_infra_parent(rate, parent_flags):
    _run(SAMPLING, parent_flags, rate=rate)


def test_export_contains_only_matched_registered_route_templates():
    _run(ROUTES)
