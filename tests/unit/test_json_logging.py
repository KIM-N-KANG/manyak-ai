"""JSON 로그 포맷(KNK-852) — manyak-server와 같은 스키마로 stdout에 내는지 고정한다.

로그 파이프라인(Fluent Bit → OpenSearch)이 두 서비스 로그를 **같은 인덱스**에 넣으므로,
필드 이름이 어긋나면 한쪽만 검색되거나 매핑이 갈린다. 백엔드 LogstashEncoder가 내는 이름
(@timestamp·@version·level·message·logger_name·service)에 맞추는 것이 이 테스트의 핵심이다.
"""

import json
import logging
import sys

from src.core.json_logging import (
    HealthCheckAccessFilter,
    JsonLogFormatter,
    configure_json_logging,
)
from src.core.request_context import set_correlation_ids


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonLogFormatter().format(record))


def _record(msg: str = "테스트 메시지", level: int = logging.INFO, **kwargs) -> logging.LogRecord:
    return logging.LogRecord(
        name=kwargs.pop("name", "src.core.sentry"),
        level=level,
        pathname="/app/src/core/sentry.py",
        lineno=1,
        msg=msg,
        args=kwargs.pop("args", ()),
        exc_info=kwargs.pop("exc_info", None),
    )


def test_백엔드와_같은_필드_이름으로_낸다():
    payload = _format(_record())

    assert payload["@timestamp"].endswith("Z")
    assert payload["@version"] == "1"
    assert payload["message"] == "테스트 메시지"
    assert payload["level"] == "INFO"
    assert payload["logger_name"] == "src.core.sentry"
    assert payload["service"] == "manyak-ai"


def test_한_줄_JSON이다():
    line = JsonLogFormatter().format(_record("여러\n줄\n메시지"))

    assert "\n" not in line
    assert json.loads(line)["message"] == "여러\n줄\n메시지"


def test_로그_인자를_먼저_적용한다():
    payload = _format(_record("값은 %s다", args=("42",)))

    assert payload["message"] == "값은 42다"


def test_상관관계_식별자를_싣는다():
    set_correlation_ids("req_abc", "sess_1", "device_hash_x")
    try:
        payload = _format(_record())
    finally:
        set_correlation_ids(None, None, None)

    assert payload["request_id"] == "req_abc"
    assert payload["session_id"] == "sess_1"
    assert payload["device_id_hash"] == "device_hash_x"


def test_식별자가_없으면_필드_자체를_넣지_않는다():
    set_correlation_ids(None, None, None)
    payload = _format(_record())

    # null로 채우면 OpenSearch에 쓸모없는 필드가 쌓인다. 아예 빼는 편이 낫다.
    assert "request_id" not in payload
    assert "session_id" not in payload
    assert "device_id_hash" not in payload


def test_예외는_stack_trace_필드로_낸다():
    try:
        raise ValueError("터졌다")
    except ValueError:
        import sys

        record = _record("실패", level=logging.ERROR, exc_info=sys.exc_info())

    payload = _format(record)

    assert payload["level"] == "ERROR"
    # 백엔드 LogstashEncoder도 예외를 stack_trace 필드에 담는다(인덱스 템플릿이 text로 매핑).
    assert "ValueError: 터졌다" in payload["stack_trace"]
    # 본문에 예외가 섞이면 message 검색이 오염된다.
    assert payload["message"] == "실패"


def test_uvicorn의_color_message는_버린다():
    record = _record("Started server process [1]", name="uvicorn.error")
    # uvicorn이 자기 핸들러용으로 붙이는 ANSI 색상 중복본. 실제 기동 로그에서 확인했다.
    record.color_message = "Started server process [\x1b[36m%d\x1b[0m]"

    payload = _format(record)

    assert "color_message" not in payload


def test_한글이_이스케이프되지_않는다():
    line = JsonLogFormatter().format(_record("한글 로그"))

    assert "한글 로그" in line


def test_핸들러는_stdout으로_낸다():
    """logging.StreamHandler()의 기본은 stderr다. 명시하지 않으면 전부 stderr로 나간다.

    백엔드 logback ConsoleAppender는 기본이 System.out이라, 맞추지 않으면 같은 인덱스에서
    두 서비스의 source 필드가 stdout/stderr로 갈린다. `uvicorn ... > logs.json` 처럼
    stdout만 받는 수집 방식에서는 로그가 통째로 사라진다(Codex P2).
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        configure_json_logging()

        assert len(root.handlers) == 1
        assert root.handlers[0].stream is sys.stdout
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    """uvicorn.access가 실제로 만드는 것과 같은 모양의 레코드.

    uvicorn은 `'%s - "%s %s HTTP/%s" %d'` 한 형식에 (클라이언트, 메서드, 경로, HTTP 버전, 상태코드)를
    args로 넘긴다. 포맷된 문자열이 아니라 이 args를 보는 것이 필터 구현의 전제다.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="uvicorn/protocols/http/httptools_impl.py",
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:33796", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_성공한_헬스체크_접근_로그는_버린다():
    """ECS 헬스체크가 15초마다 돌아 하루 5,760건이 쌓이는데 아무도 읽지 않는다(KNK-961)."""
    assert HealthCheckAccessFilter().filter(_access_record("/api/v1/health")) is False
    # uvicorn이 넘기는 경로에는 쿼리 문자열이 붙어 있을 수 있다.
    assert HealthCheckAccessFilter().filter(_access_record("/api/v1/health?probe=1")) is False


def test_다른_경로의_접근_로그는_남긴다():
    """KNK-855 실측에서 uvicorn.access가 AI 응답 완료 시각의 근거였다. 통째로 끄면 안 된다."""
    assert HealthCheckAccessFilter().filter(_access_record("/api/v1/story/storylines")) is True


def test_실패한_헬스체크는_남긴다():
    """헬스체크가 깨진 순간은 정확히 보고 싶은 신호다. 노이즈는 성공한 줄뿐이다."""
    assert HealthCheckAccessFilter().filter(_access_record("/api/v1/health", status=503)) is True


def test_접근_로그_형식이_아닌_레코드는_통과시킨다():
    """uvicorn이 WebSocket 등 다른 형식으로 남기는 줄까지 삼키지 않도록 args 모양을 확인한다."""
    assert HealthCheckAccessFilter().filter(_record("평범한 로그")) is True


def test_설정이_접근_로거에_필터를_단다():
    access_logger = logging.getLogger("uvicorn.access")
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_filters = access_logger.filters[:]
    try:
        configure_json_logging()
        configure_json_logging()  # 두 번 불러도 필터가 겹쳐 붙지 않아야 한다.

        health_filters = [f for f in access_logger.filters if isinstance(f, HealthCheckAccessFilter)]
        assert len(health_filters) == 1
        # 로거 필터는 propagate=True 여도 Logger.handle에서 먼저 걸리므로 루트 핸들러까지 가지 않는다.
        assert access_logger.filter(_access_record("/api/v1/health")) is False
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        access_logger.filters[:] = saved_filters
