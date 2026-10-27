"""ERR-09: 실제 OTel 계측을 사용하되 모든 전송은 메모리에서 검사한다."""

import subprocess
import sys

import pytest

CHECK = r'''
import asyncio
import logging
import socket
import sys
from threading import Event
from unittest.mock import patch
from uuid import UUID

# 실수로 실제 exporter를 사용해도 네트워크로 나갈 수 없다.
def deny_network(*args, **kwargs):
    raise AssertionError("external network forbidden")
socket.socket.connect = deny_network

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, AggregationTemporality
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from src.core import tracing
from src.core.config import Settings, settings

scenario = sys.argv[1]
assert Settings(_env_file=None).manyak_otlp_metrics_enabled is False
assert Settings(_env_file=None).manyak_otlp_metrics_endpoint == ""
settings.manyak_otlp_metrics_enabled = scenario != "traces_only"
settings.manyak_otlp_metrics_endpoint = "http://collector.test:4318/v1/metrics"
settings.manyak_tracing_enabled = scenario != "metrics_only"
settings.manyak_otlp_traces_endpoint = "http://collector.test:21890/v1/traces"

class MemoryExporter(MetricExporter):
    def __init__(self, **kwargs):
        super().__init__(preferred_temporality=kwargs["preferred_temporality"])
        assert set(kwargs["preferred_temporality"].values()) == {AggregationTemporality.CUMULATIVE}
        assert kwargs["endpoint"] == settings.manyak_otlp_metrics_endpoint
        self.batches = []
        self.closed = Event()
    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS
    def shutdown(self, timeout_millis=30000, **kwargs):
        self.closed.set()
    def force_flush(self, timeout_millis=10000):
        return True

exporters = []
def create_exporter(**kwargs):
    exporter = MemoryExporter(**kwargs)
    exporters.append(exporter)
    return exporter

app = FastAPI()
entered = asyncio.Event()
release = asyncio.Event()
@app.post("/work/{item}")
async def work(item: str):
    entered.set()
    await release.wait()
    return {"ok": True}
@app.get("/health")
async def health():
    return {"ok": True}

messages = []
infra_messages = []
class Capture(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())
        if record.name == tracing.logger.name:
            infra_messages.append(record.getMessage())
logging.getLogger().addHandler(Capture())
spans = InMemorySpanExporter()
metric_path = "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
trace_path = "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
reader_path = "opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader"

def assert_private_metrics_excluded(batch):
    assert "PRIVATE" not in batch.to_json()
    for resource in batch.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    assert not point.exemplars

async def requests_and_shutdown():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/health")
        if tracing._state.meter_provider:
            provider = tracing._state.meter_provider
            reader_factory.assert_called_once_with(exporters[0], export_interval_millis=60000)
            provider.force_flush()
            assert not exporters[0].batches  # health 요청만으로는 지표가 생기지 않는다
        hosts = ["PRIVATE-HOST-ONE.test", "PRIVATE-HOST-TWO.test", "PRIVATE-HOST-THREE.test"]
        pending = asyncio.create_task(client.post(
            "/work/PRIVATE-PATH?token=PRIVATE-QUERY", content="PRIVATE-BODY", headers={"host": hosts[0]},
        ))
        await asyncio.wait_for(entered.wait(), 2)
        if tracing._state.meter_provider:
            provider.force_flush()
            assert len(exporters[0].batches) == 1
            assert_private_metrics_excluded(exporters[0].batches[0])
            active = [m for r in exporters[0].batches[-1].resource_metrics for s in r.scope_metrics for m in s.metrics]
            active_points = next(m for m in active if m.name == "http.server.active_requests").data.data_points
            assert len(active_points) == 1
            assert active_points[0].value == 1
            assert dict(active_points[0].attributes) == {"http.method": "POST"}
        release.set()
        assert (await pending).status_code == 200
        for host in hosts[1:]:
            response = await client.post("/work/PRIVATE-PATH?token=PRIVATE-QUERY", headers={"host": host})
            assert response.status_code == 200
        await client.get("/PRIVATE-UNREGISTERED?secret=PRIVATE-QUERY")
        if tracing._state.meter_provider:
            provider.force_flush()
            first = exporters[0].batches[-1]
            # exemplar는 수집 때 소모되므로 두 번째 flush 전에 첫 묶음부터 검사한다.
            assert_private_metrics_excluded(first)
            provider.force_flush()
            batch = exporters[0].batches[-1]
            assert_private_metrics_excluded(batch)
            for resource in batch.resource_metrics:
                assert set(resource.resource.attributes) == {"service.name", "service.instance.id"}
                assert resource.resource.attributes["service.name"] == "manyak-ai"
                UUID(resource.resource.attributes["service.instance.id"])
            metrics = {m.name: m for r in batch.resource_metrics for s in r.scope_metrics for m in s.metrics}
            assert set(metrics) == {"http.server.duration", "http.server.active_requests"}
            duration = metrics["http.server.duration"]
            assert duration.unit == "ms"
            assert duration.data.aggregation_temporality == AggregationTemporality.CUMULATIVE
            work_points = [p for p in duration.data.data_points if p.attributes.get("http.target") == "/work/{item}"]
            assert len(work_points) == 1  # Host만 다른 요청은 같은 시계열로 합쳐진다.
            point = work_points[0]
            assert point.count == len(hosts)
            assert dict(point.attributes) == {
                "http.method": "POST", "http.status_code": 200, "http.target": "/work/{item}",
            }
            for point in duration.data.data_points:
                expected_keys = {"http.method", "http.status_code"}
                if point.attributes["http.status_code"] == 200:
                    expected_keys.add("http.target")
                assert set(point.attributes) == expected_keys
            active_points = metrics["http.server.active_requests"].data.data_points
            assert len(active_points) == 2  # POST와 등록되지 않은 경로의 GET만 구분한다.
            assert {p.attributes["http.method"] for p in active_points} == {"POST", "GET"}
            assert all(set(p.attributes) == {"http.method"} for p in active_points)
            earlier = next(m for r in first.resource_metrics for s in r.scope_metrics for m in s.metrics if m.name == duration.name)
            assert [p.count for p in earlier.data.data_points] == [p.count for p in duration.data.data_points]
            assert all(p.value == 0 for p in metrics["http.server.active_requests"].data.data_points)
        if tracing._state.provider:
            tracing._state.provider.force_flush()
            assert spans.get_finished_spans()
        else:
            assert not spans.get_finished_spans()
    original_provider = tracing._state.meter_provider
    await tracing.shutdown_tracing()
    assert tracing._state.meter_provider is None
    if exporters:
        assert exporters[0].closed.wait(1)
    if original_provider is not None:
        original_instance_id = exporters[0].batches[-1].resource_metrics[0].resource.attributes["service.instance.id"]
        replacement = tracing._create_meter_provider(settings.manyak_otlp_metrics_endpoint)
        try:
            replacement.get_meter("test.restart").create_counter("test.restart").add(1)
            replacement.force_flush()
            replacement_instance_id = exporters[-1].batches[-1].resource_metrics[0].resource.attributes["service.instance.id"]
            UUID(replacement_instance_id)
            assert replacement_instance_id != original_instance_id
        finally:
            replacement.shutdown()

with patch(metric_path, side_effect=create_exporter) as metrics_factory, patch(trace_path, return_value=spans) as traces_factory, patch(reader_path, wraps=PeriodicExportingMetricReader) as reader_factory:
    if scenario == "invalid":
        for endpoint in ("", "file:///PRIVATE", "http:///PRIVATE", "https://user:PRIVATE@host/v1/metrics", "https://host/#PRIVATE", "https://host:bad", "https://host:0", "https://@host/PRIVATE", "https://collector.test/v1/metrics?token=PRIVATE-METRIC-TOKEN"):
            settings.manyak_otlp_metrics_endpoint = endpoint
            test_app = FastAPI()
            tracing.instrument_app(test_app)
            assert tracing._state.meter_provider is None
            assert tracing._state.provider is not None  # 지표만 꺼진다.
            asyncio.run(tracing.shutdown_tracing())
        assert not metrics_factory.called
        assert len(infra_messages) == 9, messages
        assert all("PRIVATE" not in m for m in messages)
        assert all("https://collector.test/v1/metrics" not in m for m in messages)
    elif scenario == "invalid_trace_query":
        settings.manyak_otlp_traces_endpoint = "https://collector.test/v1/traces?token=PRIVATE-TRACE-TOKEN"
        tracing.instrument_app(app)
        assert tracing._state.provider is None
        assert tracing._state.meter_provider is not None  # 추적 주소 오류는 지표를 끄지 않는다.
        assert not traces_factory.called
        assert metrics_factory.called
        assert len(infra_messages) == 1, messages
        assert "인프라 추적 비활성" in infra_messages[0]
        assert all("PRIVATE" not in m for m in messages)
        assert all("https://collector.test/v1/traces" not in m for m in messages)
        asyncio.run(requests_and_shutdown())
    elif scenario in {"exporter_failure", "provider_failure", "trace_failure", "instrument_failure", "reader_failure"}:
        if scenario == "exporter_failure":
            metrics_factory.side_effect = RuntimeError("PRIVATE")
            tracing.instrument_app(app)
        elif scenario == "trace_failure":
            with patch(trace_path, side_effect=RuntimeError("PRIVATE")):
                tracing.instrument_app(app)
        elif scenario == "instrument_failure":
            with patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app", side_effect=RuntimeError("PRIVATE")):
                tracing.instrument_app(app)
            assert exporters[0].closed.wait(1)
        elif scenario == "reader_failure":
            with patch("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader", side_effect=RuntimeError("PRIVATE")):
                tracing.instrument_app(app)
            assert exporters[0].closed.wait(1)
        else:
            from opentelemetry.sdk.metrics import MeterProvider
            def fail_after_provider(**kwargs):
                assert exporters
                raise RuntimeError("PRIVATE")
            with patch.object(MeterProvider, "__init__", side_effect=fail_after_provider):
                tracing.instrument_app(app)
            assert exporters[0].closed.wait(1)
        if scenario == "trace_failure":
            assert tracing._state.provider is None and tracing._state.meter_provider is not None
        elif scenario == "instrument_failure":
            assert tracing._state.provider is None and tracing._state.meter_provider is None
        else:
            assert tracing._state.meter_provider is None and tracing._state.provider is not None
        # provider 생성 실패 후 reader의 마지막 수집은 SDK 경고도 남긴다.
        assert len(infra_messages) == 1, messages
        assert "RuntimeError" in infra_messages[0], messages
        assert all("PRIVATE" not in m for m in messages)
        asyncio.run(requests_and_shutdown())
    else:
        tracing.instrument_app(app)
        assert metrics_factory.called == (scenario != "traces_only")
        assert (tracing._state.provider is not None) == (scenario != "metrics_only")
        asyncio.run(requests_and_shutdown())
'''


@pytest.mark.parametrize("scenario", [
    "metrics_only", "traces_only", "both", "invalid", "exporter_failure",
    "provider_failure", "trace_failure", "instrument_failure", "reader_failure", "invalid_trace_query",
])
def test_infra_metrics_err09(scenario: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHECK, scenario], capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_observation_exports_disabled_before_app_import():
    from src.core.config import settings

    assert settings.manyak_otlp_metrics_enabled is False
    assert settings.manyak_otlp_metrics_endpoint == ""


async def test_meter_shutdown_uses_its_own_three_second_bound(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from src.core import tracing

    trace_shutdown, meter_shutdown = Mock(), Mock()
    monkeypatch.setattr(tracing._state, "provider", SimpleNamespace(shutdown=trace_shutdown))
    monkeypatch.setattr(tracing._state, "meter_provider", SimpleNamespace(shutdown=meter_shutdown))
    shutdown = AsyncMock()
    monkeypatch.setattr(tracing, "bounded_shutdown", shutdown)
    await tracing.shutdown_tracing()
    assert shutdown.await_args_list[0].args == (trace_shutdown,)
    assert shutdown.await_args_list[1].args == (meter_shutdown,)
    assert shutdown.await_count == 2
    assert tracing._state.provider is None and tracing._state.meter_provider is None
