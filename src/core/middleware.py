"""요청 상관관계 미들웨어 (KNK-266) — 순수 ASGI.

요청 진입 시 X-Manyak-* 상관관계 헤더를 읽어 요청별 Sentry isolation scope에 직접 싣는다
(백엔드 RequestCorrelationFilter + SentryMdcEventProcessor 대응). request_id는 tag,
session_id·anonymous_id_hash는 identity context로 — 백엔드 키명과 일치한다.

isolation scope를 쓰는 이유(설계): sentry-sdk 2.x는 요청마다 isolation scope를 만들고, 그
scope는 Starlette ServerErrorMiddleware(미처리 500을 캡처하는 최외곽)보다 바깥에서 살아 있다.
미들웨어가 진입 시 이 scope에 식별자를 심어두면 (1) capture_ai_exception 명시 캡처,
(2) chat SSE 스트리밍 중 캡처, (3) 미처리 500 자동 캡처가 모두 같은 scope를 읽어 request_id가
붙는다. (contextvar+before_send 방식은 미들웨어가 finally에서 contextvar를 reset한 뒤에야
바깥에서 500이 캡처돼 request_id가 누락됐다 — KNK-266 리뷰 F1. isolation scope는 sentry가
요청별로 관리하므로 reset 타이밍 의존이 사라진다.)

순수 ASGI(BaseHTTPMiddleware 아님)라 같은 태스크에서 app을 호출 — scope 접근이 요청 처리
전체에서 일관된다. 헤더가 없거나 "unknown"이면 아무것도 싣지 않는다(forward-compatible).
"""

import sentry_sdk
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.request_context import (
    HEADER_ANONYMOUS_ID_HASH,
    HEADER_REQUEST_ID,
    HEADER_SESSION_ID,
    KEY_ANONYMOUS_ID_HASH,
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    clean_identifier,
)


class RequestContextMiddleware:
    """X-Manyak-* 상관관계 헤더를 요청별 Sentry isolation scope에 싣는 순수 ASGI 미들웨어."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # http 외(lifespan·websocket)는 그대로 통과시킨다.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)  # 대소문자 무시 조회
        request_id = clean_identifier(headers.get(HEADER_REQUEST_ID))
        session_id = clean_identifier(headers.get(HEADER_SESSION_ID))
        anonymous_id_hash = clean_identifier(headers.get(HEADER_ANONYMOUS_ID_HASH))

        # 요청별 isolation scope에 직접 부착 — 명시 캡처·SSE·미처리 500 자동 캡처가 모두 읽는다.
        sentry_scope = sentry_sdk.get_isolation_scope()
        if request_id:
            sentry_scope.set_tag(KEY_REQUEST_ID, request_id)
        identity: dict[str, str] = {}
        if session_id:
            identity[KEY_SESSION_ID] = session_id
        if anonymous_id_hash:
            identity[KEY_ANONYMOUS_ID_HASH] = anonymous_id_hash
        if identity:
            sentry_scope.set_context("identity", identity)

        await self.app(scope, receive, send)
