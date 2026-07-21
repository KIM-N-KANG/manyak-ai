"""요청 상관관계 식별자 (KNK-266) — 헤더명·Sentry 키명·정제 헬퍼.

백엔드(manyak-server)가 매 AI 호출에 싣는 X-Manyak-* 상관관계 헤더와, Sentry에 실을 키명을
정의한다. 키명은 백엔드 MdcKeys/SentryMdcEventProcessor와 1:1로 맞춰(request_id·session_id·
device_id_hash) 양쪽 Sentry가 같은 값으로 상관관계를 잇게 한다. RequestContextMiddleware가
이 값을 읽어 요청별 Sentry isolation scope에 싣는다.

PII 안전(AN-4-10): 사용자 식별자는 원본이 아니라 해시 전용 헤더(X-Manyak-Device-Id-Hash)로만
받는다. 원본 X-Manyak-Device-Id는 상수조차 두지 않아 AI가 읽을 경로 자체가 없다.

contextvar 병존(KNK-624): Sentry는 isolation scope로 충분하지만, Langfuse 트레이스에 식별자를
실으려면 **엔드포인트 계층에서 값을 읽을 경로**가 필요하다. 그래서 미들웨어가 Sentry scope에
싣는 것과 **동시에** contextvar에도 담는다. Sentry 경로는 그대로 둔다 — contextvar 단독으로
바꿨다가 미들웨어가 reset한 뒤 캡처되는 500에서 request_id가 누락된 전례가 있다
(KNK-266 리뷰 F1, middleware.py 참조). 여기서는 reset하지 않는다: Starlette는 요청마다 별도
태스크에서 돌아 context가 요청 단위로 격리되고, 태스크 종료와 함께 사라진다.
"""

import contextvars

# 백엔드가 AI 호출에 싣는 상관관계 헤더(AN-3-3 정정판 + KNK-266 합의).
HEADER_REQUEST_ID = "X-Manyak-Request-Id"
HEADER_SESSION_ID = "X-Manyak-Session-Id"
HEADER_DEVICE_ID_HASH = "X-Manyak-Device-Id-Hash"  # 해시 전용(원본 미수신)

# Sentry에 싣는 키명 — 백엔드 MdcKeys와 동일(대시보드 상관관계 일치).
KEY_REQUEST_ID = "request_id"
KEY_SESSION_ID = "session_id"
KEY_DEVICE_ID_HASH = "device_id_hash"

# 백엔드 필터가 헤더 누락 시 채우는 sentinel. 백엔드 SentryMdcEventProcessor가 "unknown"을
# 노이즈로 보고 버리므로, AI도 동일하게 버려 양쪽 Sentry를 일치시킨다.
UNKNOWN = "unknown"


def clean_identifier(value: str | None) -> str | None:
    """빈 값·백엔드 sentinel("unknown")을 None으로 정규화한다(부착 생략용)."""
    if not value or value == UNKNOWN:
        return None
    return value


# 엔드포인트 계층이 읽는 요청 단위 식별자(Langfuse 트레이스용). 미들웨어가 요청 진입 시 채운다.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
_device_id_hash: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "device_id_hash", default=None
)


def set_correlation_ids(
    request_id: str | None, session_id: str | None, device_id_hash: str | None
) -> None:
    """요청 진입 시 상관관계 식별자를 contextvar에 싣는다(미들웨어 전용)."""
    _request_id.set(request_id)
    _session_id.set(session_id)
    _device_id_hash.set(device_id_hash)


def get_correlation_ids() -> tuple[str | None, str | None, str | None]:
    """(request_id, session_id, device_id_hash)를 읽는다. 헤더가 없었으면 각각 None."""
    return _request_id.get(), _session_id.get(), _device_id_hash.get()
