"""요청 상관관계 식별자 (KNK-266) — 헤더명·Sentry 키명·정제 헬퍼.

백엔드(manyak-server)가 매 AI 호출에 싣는 X-Manyak-* 상관관계 헤더와, Sentry에 실을 키명을
정의한다. 키명은 백엔드 MdcKeys/SentryMdcEventProcessor와 1:1로 맞춰(request_id·session_id·
anonymous_id_hash) 양쪽 Sentry가 같은 값으로 상관관계를 잇게 한다. RequestContextMiddleware가
이 값을 읽어 요청별 Sentry isolation scope에 싣는다.

PII 안전(AN-4-10): 사용자 식별자는 원본이 아니라 해시 전용 헤더(X-Manyak-Anonymous-Id-Hash)로만
받는다. 원본 X-Manyak-Anonymous-Id는 상수조차 두지 않아 AI가 읽을 경로 자체가 없다.
"""

# 백엔드가 AI 호출에 싣는 상관관계 헤더(AN-3-3 정정판 + KNK-266 합의).
HEADER_REQUEST_ID = "X-Manyak-Request-Id"
HEADER_SESSION_ID = "X-Manyak-Session-Id"
HEADER_ANONYMOUS_ID_HASH = "X-Manyak-Anonymous-Id-Hash"  # 해시 전용(원본 미수신)

# Sentry에 싣는 키명 — 백엔드 MdcKeys와 동일(대시보드 상관관계 일치).
KEY_REQUEST_ID = "request_id"
KEY_SESSION_ID = "session_id"
KEY_ANONYMOUS_ID_HASH = "anonymous_id_hash"

# 백엔드 필터가 헤더 누락 시 채우는 sentinel. 백엔드 SentryMdcEventProcessor가 "unknown"을
# 노이즈로 보고 버리므로, AI도 동일하게 버려 양쪽 Sentry를 일치시킨다.
UNKNOWN = "unknown"


def clean_identifier(value: str | None) -> str | None:
    """빈 값·백엔드 sentinel("unknown")을 None으로 정규화한다(부착 생략용)."""
    if not value or value == UNKNOWN:
        return None
    return value
