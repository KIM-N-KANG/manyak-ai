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

# 계측이 켜졌는지(init_langfuse가 활성 분기를 탔는지). observe_request가 이 값으로 분기해,
# 비활성일 때는 Langfuse SDK를 건드리지 않는다(스팬 생성·flush 모두 생략).
_enabled = False


def init_langfuse() -> None:
    """앱 시작 시 Langfuse를 초기화하고 openai 계측을 건다. 키가 비면 no-op.

    계측 import(`langfuse.openai`)를 이 함수 안에서만 하는 이유는 모듈 docstring 참조 —
    서버 기동 경로에서만 계측이 실리고 experiment·scripts에는 실리지 않게 하기 위함이다.
    """
    global _enabled
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.info("LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정 — Langfuse 비활성(no-op)")
        return

    from langfuse import Langfuse

    import langfuse.openai  # noqa: F401 — import 부작용으로 openai를 전역 계측(위 docstring)
    from sentry_sdk.integrations.logging import ignore_logger

    # 스트리밍 이탈 시 OTel detach 실패 ERROR가 Sentry에 잡히는 것을 막는다(위 docstring F1).
    ignore_logger("opentelemetry.context")

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        # 환경 구분은 Sentry와 같은 값을 쓴다 — 관측 도구 간 환경 이름이 갈리지 않게 한다.
        environment=settings.sentry_environment,
        release=settings.app_version,
    )
    _enabled = True
    logger.info(
        "Langfuse 활성 — host=%s env=%s", settings.langfuse_host, settings.sentry_environment
    )


@contextmanager
def observe_request(name: str) -> Iterator[None]:
    """요청 하나를 트레이스 하나로 묶는다 — 엔드포인트가 본 작업을 이 블록으로 감싼다.

    이 블록 안에서 일어난 LLM 호출은 전부 하위 관측으로 들어간다. 그래서 compile의 부분
    재호출(최대 3회)이나 선택지 누적 재호출도 흩어지지 않고 한 트레이스에 모인다.

    상관관계 헤더(X-Manyak-*)를 Langfuse 축으로 옮긴다 — session_id는 대화 묶음 검색,
    user_id는 사용자별 집계, request_id는 백엔드 로그·Sentry와의 교차 조회에 쓴다(SDK가
    상관관계 식별자는 tags가 아니라 metadata에 두라고 명시한다). 값이 없으면(로컬 호출 등)
    그 축만 빠지고 트레이스는 정상 생성된다.

    비활성(키 미설정)이면 아무 스팬도 만들지 않고 그대로 통과한다 — Langfuse SDK를 import조차
    하지 않은 상태이므로 여기서 건드리면 안 된다.
    """
    if not _enabled:
        yield
        return

    from langfuse import get_client, propagate_attributes

    request_id, session_id, device_id_hash = get_correlation_ids()
    client = get_client()
    with client.start_as_current_observation(name=name, as_type="span"):
        with propagate_attributes(
            session_id=session_id,
            user_id=device_id_hash,  # 원본 기기 식별자가 아니라 해시다(AN-4-10)
            trace_name=name,
            # 상관관계 식별자는 metadata에 둔다 — tags는 저카디널리티 분류축이라 요청마다
            # 유일한 값을 넣으면 필터 목록이 폭발한다(SDK docstring 권고).
            metadata={"request_id": request_id} if request_id else None,
        ):
            yield


def shutdown_langfuse() -> None:
    """앱 종료 시 미전송 트레이스를 밀어낸다.

    Langfuse는 배치 전송이라 flush 없이 프로세스가 죽으면 마지막 구간의 관측이 유실된다.
    컨테이너 재시작·배포마다 공백이 생기지 않도록 lifespan 종료 훅에서 부른다. 비활성이면
    Langfuse SDK를 import하지 않았으므로 아무것도 하지 않는다.
    """
    if not _enabled:
        return
    from langfuse import get_client

    get_client().flush()
