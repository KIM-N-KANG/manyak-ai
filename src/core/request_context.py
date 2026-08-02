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
import logging
from collections.abc import Mapping
from typing import TypeAlias

logger = logging.getLogger(__name__)

# 백엔드가 AI 호출에 싣는 상관관계 헤더(AN-3-3 정정판 + KNK-266 합의).
HEADER_REQUEST_ID = "X-Manyak-Request-Id"
HEADER_SESSION_ID = "X-Manyak-Session-Id"
HEADER_DEVICE_ID_HASH = "X-Manyak-Device-Id-Hash"  # 해시 전용(원본 미수신)

# Langfuse 생성 결과 연결용 헤더(4-backend §4-7, 5-ai-server §5-6).
HEADER_CREATION_ID = "X-Manyak-Creation-Id"
HEADER_PARENT_CREATION_ID = "X-Manyak-Parent-Creation-Id"
HEADER_STORYLINE_ID = "X-Manyak-Storyline-Id"
HEADER_STORYLINE_ORDER = "X-Manyak-Storyline-Order"
HEADER_STORY_ID = "X-Manyak-Story-Id"
HEADER_CHAT_ID = "X-Manyak-Chat-Id"
HEADER_START_SETTING_ID = "X-Manyak-Start-Setting-Id"
HEADER_TURN_NUMBER = "X-Manyak-Turn-Number"
HEADER_IS_REGENERATED = "X-Manyak-Is-Regenerated"

# Sentry에 싣는 키명 — 백엔드 MdcKeys와 동일(대시보드 상관관계 일치).
KEY_REQUEST_ID = "request_id"
KEY_SESSION_ID = "session_id"
KEY_DEVICE_ID_HASH = "device_id_hash"

# 백엔드 필터가 헤더 누락 시 채우는 sentinel. 백엔드 SentryMdcEventProcessor가 "unknown"을
# 노이즈로 보고 버리므로, AI도 동일하게 버려 양쪽 Sentry를 일치시킨다.
UNKNOWN = "unknown"

ConnectionMetadataValue: TypeAlias = str | int | bool
ConnectionMetadata: TypeAlias = dict[str, ConnectionMetadataValue]

_STRING_CONNECTION_HEADERS = {
    HEADER_CREATION_ID: "creation_id",
    HEADER_PARENT_CREATION_ID: "parent_creation_id",
    HEADER_STORY_ID: "story_id",
    HEADER_CHAT_ID: "chat_id",
    HEADER_START_SETTING_ID: "start_setting_id",
}
_INTEGER_CONNECTION_HEADERS = {
    HEADER_STORYLINE_ID: ("storyline_id", 1, 9_223_372_036_854_775_807),
    HEADER_STORYLINE_ORDER: ("storyline_order", 1, 3),
    HEADER_TURN_NUMBER: ("turn_number", 1, 2_147_483_647),
}


def clean_identifier(value: str | None) -> str | None:
    """빈 값·백엔드 sentinel("unknown")을 None으로 정규화한다(부착 생략용)."""
    if not value or value == UNKNOWN:
        return None
    return value


def _clean_connection_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned == UNKNOWN:
        return None
    return cleaned


def parse_connection_metadata(headers: Mapping[str, str]) -> ConnectionMetadata:
    """백엔드 연결 헤더를 Langfuse metadata 타입으로 정규화한다.

    누락·공백·unknown은 조용히 생략하고, 숫자·불리언 형식 오류는 해당 필드만 버린다.
    관측용 헤더 하나가 잘못돼도 사용자 요청을 막지 않는다.
    """
    metadata: ConnectionMetadata = {}

    for header, key in _STRING_CONNECTION_HEADERS.items():
        value = _clean_connection_value(headers.get(header))
        if value is not None:
            metadata[key] = value

    for header, (key, minimum, maximum) in _INTEGER_CONNECTION_HEADERS.items():
        value = _clean_connection_value(headers.get(header))
        if value is None:
            continue
        if not value.isascii() or not value.isdecimal():
            logger.warning("Langfuse 연결 헤더 값 무시 — %s는 정수여야 함", header)
            continue
        try:
            parsed = int(value)
        except ValueError:
            logger.warning("Langfuse 연결 헤더 값 무시 — %s는 정수여야 함", header)
            continue
        if not minimum <= parsed <= maximum:
            logger.warning(
                "Langfuse 연결 헤더 값 무시 — %s는 %d~%d 범위여야 함",
                header,
                minimum,
                maximum,
            )
            continue
        metadata[key] = parsed

    regenerated = _clean_connection_value(headers.get(HEADER_IS_REGENERATED))
    if regenerated is not None:
        normalized = regenerated.lower()
        if normalized == "true":
            metadata["is_regenerated"] = True
        elif normalized == "false":
            metadata["is_regenerated"] = False
        else:
            logger.warning(
                "Langfuse 연결 헤더 값 무시 — %s는 true 또는 false여야 함",
                HEADER_IS_REGENERATED,
            )

    return metadata


# 엔드포인트 계층이 읽는 요청 단위 식별자(Langfuse 트레이스용). 미들웨어가 요청 진입 시 채운다.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
_device_id_hash: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "device_id_hash", default=None
)
_connection_metadata: contextvars.ContextVar[ConnectionMetadata | None] = contextvars.ContextVar(
    "connection_metadata", default=None
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


def set_connection_metadata(metadata: ConnectionMetadata) -> None:
    """요청 진입 시 정규화한 연결 metadata를 contextvar에 싣는다(미들웨어 전용)."""
    _connection_metadata.set(dict(metadata))


def get_connection_metadata() -> ConnectionMetadata:
    """현재 요청의 연결 metadata를 복사해 반환한다. 없으면 빈 dict다."""
    return dict(_connection_metadata.get() or {})


def select_connection_metadata(*allowed_keys: str) -> ConnectionMetadata:
    """현재 요청의 연결 metadata에서 해당 API가 허용한 키만 반환한다."""
    metadata = get_connection_metadata()
    return {key: metadata[key] for key in allowed_keys if key in metadata}
