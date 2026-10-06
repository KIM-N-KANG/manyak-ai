"""원문을 수집하지 않는 인프라 추적. Langfuse의 current span과 부모를 공유하지 않는다.

OTel은 활성화 함수 안에서만 import한다. 수동 스팬은 요청에서 저장한 부모를 명시하고
current scope를 열지 않는다. 따라서 async generator의 yield·다른 태스크에서의 close에도
컨텍스트 토큰을 잘못 detach하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)
_MANUAL_KEYS = frozenset({"provider", "model", "operation", "status", "error.type"})
_HTTP_KEYS = frozenset({"http.method", "http.request.method", "http.status_code", "http.response.status_code"})
_NAMES = frozenset({"llm.complete", "llm.stream", "image.generate", "image.download", "image.upload"})
_IDENTIFIER = re.compile(r"[a-zA-Z0-9_.:/-]{1,128}\Z")
_request_parent: ContextVar[Context | None] = ContextVar("infra_request_parent", default=None)


class _TracingState:
    provider: TracerProvider | None = None


_state = _TracingState()


def _attributes(attributes: Mapping[str, object]) -> dict[str, str]:
    """자유 텍스트·URL·본문 대신 코드에서 선택한 기술 식별자만 허용한다."""
    return {
        key: value for key, value in attributes.items()
        if key in _MANUAL_KEYS and isinstance(value, str)
        and _IDENTIFIER.fullmatch(value) and "://" not in value
    }


def _create_provider(exporter: SpanExporter) -> TracerProvider:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, SpanKind, Status

    class SafeProcessor(SpanProcessor):
        """자동 계측도 최종 전송 전에 허용 목록을 적용한다. 원본 span은 변경하지 않는다."""

        def __init__(self) -> None:
            self.batch = BatchSpanProcessor(exporter)

        def on_end(self, span: ReadableSpan) -> None:
            # 요청 스팬과 명시적인 수동 작업만 전송한다(BackgroundTask 자동 스팬 제외).
            if span.kind != SpanKind.SERVER and span.name not in _NAMES:
                return
            try:
                attributes: dict[str, str | int] = dict(_attributes(span.attributes or {}))
                for key, value in (span.attributes or {}).items():
                    if key in _HTTP_KEYS and isinstance(value, (str, int)):
                        attributes[key] = value
                safe = ReadableSpan(
                    name="HTTP request" if span.kind == SpanKind.SERVER else span.name,
                    # tracestate도 외부 입력이므로 전송 복사본에는 ID와 sampling flag만 남긴다.
                    context=SpanContext(
                        trace_id=span.context.trace_id, span_id=span.context.span_id,
                        is_remote=False, trace_flags=span.context.trace_flags,
                    ), parent=span.parent, kind=span.kind,
                    resource=Resource({"service.name": "manyak-ai"}),
                    attributes=attributes, events=(), links=(),
                    status=Status(span.status.status_code),
                    start_time=span.start_time, end_time=span.end_time,
                    instrumentation_scope=InstrumentationScope("manyak.infra"),
                )
                self.batch.on_end(safe)
            except Exception as exc:  # 정제 실패 시 전송하지 않고 응답은 보존한다.
                logger.warning("인프라 스팬 정제 실패(%s)", type(exc).__name__)

        def shutdown(self) -> None:
            self.batch.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return self.batch.force_flush(timeout_millis)

    provider = TracerProvider(
        resource=Resource({"service.name": "manyak-ai"}),
        sampler=ParentBased(ALWAYS_ON), shutdown_on_exit=False,
    )
    provider.add_span_processor(SafeProcessor())
    return provider


class _RequestParentMiddleware:
    """ASGI 응답 스트림이 끝날 때까지 요청 부모를 보존하고 요청마다 복원한다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from opentelemetry.context import Context
        from opentelemetry.trace import set_span_in_context

        span = scope.get("manyak.infra_span")
        parent = set_span_in_context(span, Context()) if span is not None else None
        token = _request_parent.set(parent)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_parent.reset(token)


def instrument_app(app: FastAPI) -> None:
    """기동 실패를 격리한다. off/잘못된 URL이면 OTel을 불러오지도 않는다."""
    from src.core.config import settings

    if not settings.manyak_tracing_enabled:
        return
    endpoint = settings.manyak_otlp_traces_endpoint
    try:
        url = urlsplit(endpoint)
        valid = url.scheme in {"http", "https"} and bool(url.hostname) and url.port != 0
        valid = valid and not url.username and not url.password and not url.fragment
    except ValueError:
        valid = False
    if not valid:
        # 설정 값은 자격증명/쿼리일 수도 있어 로그에 싣지 않는다.
        logger.error("인프라 추적 비활성: MANYAK_OTLP_TRACES_ENDPOINT에 전체 HTTP(S) URL이 필요합니다")
        return
    if _state.provider is not None:
        return
    provider = None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.metrics import NoOpMeterProvider
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        provider = _create_provider(OTLPSpanExporter(endpoint=endpoint, timeout=2))
        set_global_textmap(TraceContextTextMapPropagator())

        def save_request_span(span: object, scope: dict[str, object]) -> None:
            scope["manyak.infra_span"] = span

        app.add_middleware(_RequestParentMiddleware)
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, meter_provider=NoOpMeterProvider(),
            excluded_urls="health", exclude_spans=["receive", "send"],
            server_request_hook=save_request_span,
        )
        _state.provider = provider
    except Exception as exc:  # 관측 초기화 실패가 앱 기동을 막지 않도록 격리한다.
        logger.error("인프라 추적 초기화 실패(%s): 비활성으로 기동", type(exc).__name__)
        if provider is not None:
            Thread(target=provider.shutdown, daemon=True).start()


@contextmanager
def start_span(name: str, attributes: Mapping[str, object]) -> Iterator[None]:
    """current scope 없이 한 작업을 기록한다. 취소·GeneratorExit도 한 번만 end한다."""
    if _state.provider is None or name not in _NAMES or _request_parent.get() is None:
        yield
        return
    from opentelemetry.trace import Status, StatusCode

    try:
        span = _state.provider.get_tracer("manyak.infra").start_span(
            name, context=_request_parent.get(), attributes=_attributes(attributes),
        )
    except Exception as exc:  # 계측 시작 실패는 실제 공급자 호출에 전파하지 않는다.
        logger.warning("인프라 스팬 시작 실패(%s)", type(exc).__name__)
        yield
        return
    error_type = None
    try:
        yield
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        try:
            try:
                if error_type:
                    span.set_attribute("error.type", error_type)
                    span.set_attribute("status", "error")
                    span.set_status(Status(StatusCode.ERROR))
                else:
                    span.set_attribute("status", "ok")
            finally:
                span.end()
        except Exception as exc:  # 정리 실패가 원래 취소/공급자 오류를 덮지 않게 한다.
            logger.warning("인프라 스팬 종료 실패(%s)", type(exc).__name__)


def log_context() -> dict[str, str]:
    """Langfuse가 current여도 저장한 인프라 요청 ID를 사용한다. off면 import 없음."""
    if _state.provider is None or _request_parent.get() is None:
        return {}
    from opentelemetry.trace import get_current_span

    context = get_current_span(_request_parent.get()).get_span_context()
    if not context.is_valid:
        return {}
    return {"traceId": f"{context.trace_id:032x}", "spanId": f"{context.span_id:016x}"}


async def bounded_shutdown(callback: Callable[[], None], timeout: float = 3) -> None:
    """네트워크 flush가 이벤트 루프/기동 종료를 무기한 붙잡지 않게 한다."""
    finished = Event()

    def run() -> None:
        try:
            callback()
        except Exception as exc:  # 종료 관측 실패만 격리한다.
            logger.warning("관측 종료 실패(%s)", type(exc).__name__)
        finally:
            finished.set()

    Thread(target=run, daemon=True).start()
    deadline = asyncio.get_running_loop().time() + timeout
    while not finished.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            logger.warning("관측 종료 대기 시간 초과: 마지막 배치가 유실될 수 있습니다")
            return
        await asyncio.sleep(0.02)


async def shutdown_tracing() -> None:
    provider = _state.provider
    _state.provider = None
    if provider is not None:
        await bounded_shutdown(provider.shutdown)
