"""JSON 한 줄 로그 포맷(KNK-852).

로그 파이프라인(Fluent Bit → OpenSearch)이 manyak-server와 이 서비스의 로그를 **같은 인덱스**에
넣는다. 그래서 필드 이름을 백엔드 LogstashEncoder 출력에 맞춘다. 이름이 어긋나면 한쪽만 검색되거나
인덱스 매핑이 갈린다.

백엔드가 내는 필드(manyak-server `logback-spring.xml`):
    @timestamp, @version, message, logger_name, thread_name, level, level_value, service
    + MDC(request_id, session_id, device_id_hash) + 예외 시 stack_trace

의존성을 더하지 않고 표준 라이브러리로 쓴다. 필요한 일이 dict를 만들어 json.dumps 하는 것뿐이라
로깅 라이브러리를 하나 더 들일 이유가 없다.
"""

import datetime as dt
import json
import logging
import sys

from src.core.request_context import get_correlation_ids

SERVICE_NAME = "manyak-ai"

# LogRecord의 기본 속성 이름. extra= 로 들어온 사용자 필드만 골라내는 데 쓴다.
# color_message는 uvicorn이 자기 핸들러용으로 붙이는 ANSI 색상 중복본이라 뺀다. 남겨 두면
# 이스케이프 문자가 든 필드가 OpenSearch 매핑에 생기고 message와 내용이 겹친다(실제 기동 로그에서 확인).
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"asctime", "message", "taskName", "color_message"}
)


# 컨테이너·ALB 헬스체크가 두드리는 경로. 라우터 prefix(`/api/v1`) + health 라우트다.
_HEALTH_PATH = "/api/v1/health"

# uvicorn.access는 `'%s - "%s %s HTTP/%s" %d'` 한 형식에 (클라이언트, 메서드, 경로, HTTP 버전,
# 상태코드)를 args로 넘긴다. 포맷된 문자열을 다시 파싱하지 않고 이 args를 본다.
_ACCESS_LOG_ARG_COUNT = 5
_PATH_INDEX = 2
_STATUS_INDEX = 4


class HealthCheckAccessFilter(logging.Filter):
    """성공한 헬스체크 접근 로그를 버린다(KNK-961).

    헬스체크는 15초마다 돌아 하루 5,760건이 쌓이는데 아무도 읽지 않는다. 색인 용량을 먹고
    Discover에서 실제 로그를 밀어낸다(백엔드도 같은 이유로 KNK-851에서 걷어냈다).

    `uvicorn.access` 로거를 통째로 끄지는 않는다 — KNK-855 실측에서 이 로거가 AI 응답 완료
    시각의 근거였다. 실패한 헬스체크(4xx·5xx)도 남긴다. 그건 노이즈가 아니라 정확히 보고 싶은
    신호다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # WebSocket 등 형식이 다른 줄까지 삼키지 않도록 접근 로그 모양일 때만 판단한다.
        if not isinstance(args, tuple) or len(args) != _ACCESS_LOG_ARG_COUNT:
            return True
        status = args[_STATUS_INDEX]
        if not isinstance(status, int) or status >= 400:
            return True
        # uvicorn이 넘기는 경로에는 쿼리 문자열이 붙어 있을 수 있다.
        return str(args[_PATH_INDEX]).split("?", 1)[0] != _HEALTH_PATH


class JsonLogFormatter(logging.Formatter):
    """LogRecord를 백엔드와 같은 스키마의 JSON 한 줄로 만든다."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # 백엔드는 나노초까지 내지만 OpenSearch date 타입이 밀리초로 절삭하므로 여기선 밀리초로 충분하다.
            "@timestamp": dt.datetime.fromtimestamp(record.created, dt.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "@version": "1",
            "message": record.getMessage(),
            "logger_name": record.name,
            "thread_name": record.threadName,
            "level": record.levelname,
            "level_value": record.levelno * 1000,
            "service": SERVICE_NAME,
        }

        # 백엔드 RequestCorrelationFilter가 MDC에 넣는 것과 같은 세 값. 미들웨어가 요청마다 채운다.
        # 없으면 키 자체를 빼서 OpenSearch에 빈 필드가 쌓이지 않게 한다.
        request_id, session_id, device_id_hash = get_correlation_ids()
        for key, value in (
            ("request_id", request_id),
            ("session_id", session_id),
            ("device_id_hash", device_id_hash),
        ):
            if value:
                payload[key] = value

        # 예외는 message가 아니라 별도 필드로 낸다. 섞으면 message 전문 검색이 스택 트레이스로 오염된다.
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["stack_trace"] = record.exc_text
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # logger.info("...", extra={"event_name": ...}) 로 넘긴 값을 최상위 필드로 올린다.
        # 백엔드 StructuredArguments와 같은 자리다.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        # ensure_ascii=False 라야 한글이 \uXXXX 로 부풀지 않는다.
        # 한 줄이어야 하므로 개행이 들어간 값은 json.dumps 가 \n 으로 이스케이프한다(기본 동작).
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(level: int = logging.INFO) -> None:
    """루트 로거를 JSON 포매터 하나만 달린 stdout 핸들러로 바꾼다.

    uvicorn은 자기 로거(`uvicorn`, `uvicorn.access`)에 핸들러를 직접 달고 propagate=False 로 두므로,
    루트만 바꾸면 접근 로그가 평문으로 남는다. 그래서 그 로거들의 핸들러를 떼어 루트로 흘려보낸다.

    stdout을 명시하는 이유: `logging.StreamHandler()`의 기본은 **stderr**다. 그냥 두면 앱과 uvicorn
    로그가 전부 stderr로 나가, `uvicorn ... > logs.json` 처럼 stdout만 받는 수집에서는 한 줄도 안 남는다.
    백엔드 logback ConsoleAppender는 기본이 System.out 이라, 맞추지 않으면 같은 인덱스에서 두 서비스의
    source 필드가 stdout/stderr 로 갈린다.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # 필터는 핸들러가 아니라 로거에 단다. Logger.handle이 상위로 전파하기 **전에** 걸러 주므로,
    # 핸들러를 루트로 옮긴 위 설정과 무관하게 동작한다. 두 번 호출돼도 겹쳐 붙지 않게 정리한다.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters[:] = [
        f for f in access_logger.filters if not isinstance(f, HealthCheckAccessFilter)
    ]
    access_logger.addFilter(HealthCheckAccessFilter())
