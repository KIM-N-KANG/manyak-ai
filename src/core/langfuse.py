"""Langfuse LLM 관측 초기화 (KNK-624).

서비스가 부른 LLM 호출의 프롬프트·응답 원문과 토큰·지연을 Langfuse에 남긴다. 이 모듈이
계측을 **켜는 유일한 지점**이고(`init_langfuse`), 트레이스로 묶는 도구(`observe_request`)와
종료 flush(`shutdown_langfuse`)를 함께 둔다.

**계측을 왜 여기서만 켜나 (핵심 설계).** `langfuse.openai`는 import되는 순간 `openai` 모듈의
`AsyncCompletions.create`를 프로세스 전역으로 monkey-patch한다 — 서브클래스가 아니라 원본
클래스 메서드를 바꾼다. 그래서 서비스 모듈이 `from langfuse.openai import ...`로 바꾸면 그
파일을 import하는 **모든** 경로(운영 서버뿐 아니라 experiment 러너·scripts 로컬 도구)가 계측을
물려받는다. 그건 원치 않는다 — experiment는 자체 기록 체계가 있어 이중 기록이 되고, 러너는
init_langfuse를 부르지 않으므로 키 처리도 안 된다.

그래서 서비스 모듈은 순정 `openai`를 쓰고, 계측 import는 **여기 init_langfuse 안에서만** 한다.
init_langfuse는 앱 기동 경로(main.py)에서만 불리므로, 서버를 띄우지 않고 함수를 직접 부르는
experiment·scripts에는 계측이 아예 실리지 않는다. 뒤늦은 import여도 이미 만들어진 클라이언트에
패치가 걸린다(monkey-patch가 인스턴스가 아니라 클래스 메서드를 바꾸기 때문).

**원문 취급은 Sentry와 정반대.** Sentry는 원문을 싣지 않지만(AN-4-10), Langfuse는 원문을 싣는
것이 도입 목적이다. 원문 비수집 원칙의 명시적 예외이며, 6-analytics §6-7 개정이 짝이다(KNK-634).

**켜고 끄기.** 키가 비면 no-op이라 로컬·CI는 관측 때문에 외부 의존이 생기지 않는다. no-op일
때는 계측 import 자체를 하지 않으므로, 순정 openai가 그대로 남아 경고 소음도 없다.

**활성화 가드 (KNK-652).** 원문 수집은 6-analytics §6-7이 "prod 전용·JP 리전 저장" 조건 아래
허용한 예외다. 키 유무만 보고 켜면 키가 다른 환경에 흘러들거나 HOST를 빠뜨렸을 때(기본값이
JP가 아님) 허용 조건 밖에서 원문이 수집되므로, 키가 있어도 host가 JP 엔드포인트이고 환경이
prod일 때만 켠다(5-ai-server §5-6). 미충족이면 기동을 막지 않고 no-op + 오류 로그 — 관측
실패가 서비스를 깨면 안 된다는 원칙과 같다. 로컬 통합 검증이 필요하면 로컬 `.env`의
환경값을 의식적으로 prod로 바꿔 켠다(기본은 차단).

**스트리밍 이탈과 Sentry(중요).** chat 턴은 async generator로 SSE를 흘리는데, 사용자가 스트림
도중 연결을 끊으면 Starlette가 제너레이터를 yield에 매단 채 버리고, asyncio가 나중에 **다른
태스크**에서 파이널라이즈한다. 그때 OTel이 "다른 컨텍스트에서 만든 토큰"이라며 context detach에
실패해 `opentelemetry.context` 로거로 ERROR를 남긴다(스팬은 정상 종료되므로 기능 피해는 없다).
문제는 Sentry LoggingIntegration이 이 ERROR를 이벤트로 잡아, 흔한 중도 이탈마다 오류가 쌓이는
것이다. 그래서 활성화 시 이 로거를 Sentry 포획에서 제외한다 — 로컬 로그에는 그대로 남고 Sentry
이벤트만 막는다(KNK-624 리뷰 F1).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from src.core.config import settings
from src.core.request_context import get_correlation_ids

logger = logging.getLogger(__name__)

# 원문 수집이 허용된 유일한 저장 리전(JP)과 환경(prod) — 6-analytics §6-7 예외 조건.
_ALLOWED_HOST = "https://jp.cloud.langfuse.com"
_ALLOWED_ENVIRONMENT = "prod"

# 계측이 켜졌는지(init_langfuse가 활성 분기를 탔는지). observe_request가 이 값으로 분기해,
class _LangfuseState:
    """계측 활성 여부를 담는 상태 객체.

    모듈 전역 가변 변수(`global`)는 스타일 가이드 §7 금지 패턴이라, 상태를 객체 속성으로
    캡슐화한다. observe_request·shutdown_langfuse가 이 값으로 분기해, 비활성일 때는 Langfuse
    SDK를 건드리지 않는다(스팬 생성·flush 모두 생략).
    """

    enabled: bool = False


_state = _LangfuseState()


def init_langfuse() -> None:
    """앱 시작 시 Langfuse를 초기화하고 openai 계측을 건다. 키가 비거나 JP·prod 조건 미충족이면 no-op.

    계측 import(`langfuse.openai`)를 이 함수 안에서만 하는 이유는 모듈 docstring 참조 —
    서버 기동 경로에서만 계측이 실리고 experiment·scripts에는 실리지 않게 하기 위함이다.
    """
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.info("LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정 — Langfuse 비활성(no-op)")
        return

    # 활성화 가드(KNK-652): 키가 있어도 JP·prod가 아니면 켜지 않는다(모듈 docstring).
    # 기동은 막지 않는다 — 관측 설정 오류가 서비스를 죽이면 안 된다.
    # 후행 슬래시는 여기서 한 번 정규화해 비교·SDK 전달에 같은 값을 쓴다(비대칭 방지).
    host = settings.langfuse_host.rstrip("/")
    if host != _ALLOWED_HOST:
        logger.error(
            "Langfuse 비활성 — LANGFUSE_HOST가 JP 엔드포인트(%s)가 아님: %s "
            "(원문 수집은 JP 리전만 허용, 5-ai-server §5-6)",
            _ALLOWED_HOST,
            settings.langfuse_host,
        )
        return
    if settings.sentry_environment != _ALLOWED_ENVIRONMENT:
        logger.error(
            "Langfuse 비활성 — 환경이 prod가 아님: %s "
            "(원문 수집은 prod 전용, 5-ai-server §5-6. 로컬 검증은 .env 환경값을 의식적으로 변경)",
            settings.sentry_environment,
        )
        return

    from langfuse import Langfuse

    import langfuse.openai  # noqa: F401 — import 부작용으로 openai를 전역 계측(위 docstring)
    from sentry_sdk.integrations.logging import ignore_logger

    # 스트리밍 이탈 시 OTel detach 실패 ERROR가 Sentry에 잡히는 것을 막는다(위 docstring F1).
    ignore_logger("opentelemetry.context")

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=host,  # 가드가 검증한 정규화 값 — 비교와 전달이 같은 값이어야 한다
        # 환경 구분은 Sentry와 같은 값을 쓴다 — 관측 도구 간 환경 이름이 갈리지 않게 한다.
        environment=settings.sentry_environment,
        release=settings.app_version,
    )
    _state.enabled = True
    logger.info("Langfuse 활성 — host=%s env=%s", host, settings.sentry_environment)


def dimension_tags(
    *,
    genre_tags: list[str] | None = None,
) -> list[str]:
    """선호 분석용 저카디널리티 태그를 만든다 — **장르만**. 접두사로 축을 구분한다.

    장르는 스토리 제작 시점에만 선택되므로 **스토리 제작 트레이스(스토리라인·컴파일)에만**
    싣는다 — 채팅 턴·선택지의 단일 `genre` 인자는 KNK-652에서 제거했다(채팅 트레이스 장르는
    후속, 5-ai-server §5-6). story `genre_tags`는 키워드 단계 개편(KNK-621)이 커스텀 장르
    입력을 400으로 차단하면 사전 정의만 남는다 — 켜기는 그 배포 이후로 순서를 잡는다.
    주인공·조연 태그(protagonist_tags·supporting_tags)는 사용자가 직접 키워드를 입력해
    추가할 수 있어(US-3-3, 4-backend §4-4) **원문이 섞이고 카디널리티가 폭발**한다 — 그래서
    인자 자체를 두지 않아 트레이스 태그로 새어 나갈 경로를 없앴다(AN-4-10·§6-7 원문 비수집,
    KNK-640 리뷰). 사용자 자유입력(user_input·additional_info)도 마찬가지.
    """
    return [f"genre:{g}" for g in (genre_tags or [])]


class _Trace:
    """observe_request가 넘기는 핸들. 트레이스에 실을 분석 metadata를 모아 블록 끝에서 한 번에 기록한다.

    장르는 tags로 미리 싣지만, 프롬프트 버전·재호출 횟수는 metadata로 이 핸들에 모은다. 재호출
    횟수는 compile·choices에서 작업이 끝나야 확정되므로, 미리 아는 값과 사후 값을 **한 버퍼에 모아
    루트 관측 metadata로 1회 기록**한다 — 경로마다 위치·타입이 갈리지 않게 하고(트레이스 metadata는
    모든 값을 문자열로 뭉개지만 관측 metadata는 int·JSON을 보존한다), update 호출도 1회로 줄인다.

    비활성이면 span이 None이라 아무 일도 하지 않는다.
    """

    def __init__(self, span: object | None = None) -> None:
        self._span = span
        self._metadata: dict[str, object] = {}

    def set_metadata(self, **kwargs: object) -> None:
        """분석 metadata를 버퍼에 모은다. 실제 기록은 블록 종료 시 _flush가 1회 수행한다."""
        self._metadata.update(kwargs)

    def _flush(self) -> None:
        if self._span is None or not self._metadata:
            return
        try:
            self._span.update(metadata=self._metadata)
        except Exception:  # noqa: BLE001 — 관측 기록 실패가 서비스 응답을 깨면 안 된다
            logger.warning("Langfuse metadata 기록 실패 — 트레이스만 누락, 응답에는 영향 없음", exc_info=True)


@contextmanager
def observe_request(
    name: str,
    *,
    tags: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> Iterator[_Trace]:
    """요청 하나를 트레이스 하나로 묶는다 — 엔드포인트가 본 작업을 이 블록으로 감싼다.

    이 블록 안에서 일어난 LLM 호출은 전부 하위 관측으로 들어간다. 그래서 compile의 부분
    재호출(최대 3회)이나 선택지 누적 재호출도 흩어지지 않고 한 트레이스에 모인다.

    트레이스 정체성(session_id·user_id·tags·trace_name)은 propagate_attributes로 싣는다 —
    session_id는 대화 묶음 검색, user_id는 사용자별 집계(원본 아닌 기기 해시), tags는 장르 필터.
    request_id·프롬프트 버전·재호출 횟수 같은 분석 metadata는 핸들 버퍼에 모아 블록 끝에서 루트
    관측에 1회 기록한다(위치·타입 일관·update 1회 — _Trace 참조). 값이 없으면 그 축만 빠지고
    트레이스는 정상 생성된다.

    비활성(키 미설정 또는 JP·prod 조건 미충족)이면 아무 스팬도 만들지 않고 그대로 통과한다 —
    Langfuse SDK를 import조차 하지 않은 상태이므로 여기서 건드리면 안 된다.
    """
    if not _state.enabled:
        yield _Trace()
        return

    from langfuse import get_client, propagate_attributes

    request_id, session_id, device_id_hash = get_correlation_ids()
    client = get_client()
    with client.start_as_current_observation(name=name, as_type="span") as span:
        with propagate_attributes(
            session_id=session_id,
            user_id=device_id_hash,  # 원본 기기 식별자가 아니라 해시다(AN-4-10)
            trace_name=name,
            tags=tags or None,
        ):
            trace = _Trace(span)
            if request_id:
                trace.set_metadata(request_id=request_id)
            if metadata:
                trace.set_metadata(**metadata)
            try:
                yield trace
            finally:
                trace._flush()  # 미리 값 + 사후 값(retry_count)을 모아 1회 기록


def shutdown_langfuse() -> None:
    """앱 종료 시 미전송 트레이스를 밀어낸다.

    Langfuse는 배치 전송이라 flush 없이 프로세스가 죽으면 마지막 구간의 관측이 유실된다.
    컨테이너 재시작·배포마다 공백이 생기지 않도록 lifespan 종료 훅에서 부른다. 비활성이면
    Langfuse SDK를 import하지 않았으므로 아무것도 하지 않는다.
    """
    if not _state.enabled:
        return
    from langfuse import get_client

    get_client().flush()
