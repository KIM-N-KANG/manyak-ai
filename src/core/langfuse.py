"""Langfuse LLM 관측 초기화 (KNK-624).

서비스가 부른 LLM 호출의 프롬프트·응답 원문과 토큰·지연을 Langfuse에 남긴다. 계측 자체는
`langfuse.openai` 드롭인이 하고(서비스 모듈의 import 한 줄), 이 모듈은 **클라이언트 수명주기만**
맡는다 — 앱 시작 시 초기화, 종료 시 flush.

Sentry(`src/core/sentry.py`)와 켜고 끄는 규약을 맞춘다: 키가 비면 비활성(no-op)이라 로컬·CI는
관측 때문에 외부 의존이 생기지 않는다. 다만 **원문 취급은 정반대**다 — Sentry는 원문을 싣지
않지만(AN-4-10), Langfuse는 원문을 싣는 것이 도입 목적이다. 원문 비수집 원칙의 명시적 예외이며
6-analytics §6-7 개정이 짝이다.

초기화가 필수인 이유: `langfuse.openai`는 클라이언트가 없으면 **환경변수**(LANGFUSE_*)를 읽어
스스로 만든다. 우리 키는 .env → Settings로 들어가지 os.environ에는 없으므로, 이 함수가 명시적으로
만들어주지 않으면 계측이 조용히 꺼진다(운영 검증에서 실제로 겪었다).

no-op 구현 메모: 키 없이 첫 호출이 나가면 자동 생성이 "Authentication error ... Client will be
disabled"를 stderr에 찍는다(LLM 호출 자체는 정상 동작한다). 로컬·CI 실행마다 이 소음이 나므로,
키가 없을 때는 자리표시 키 + tracing_enabled=False로 싱글턴을 **선점**해 조용히 끈다. 자리표시
키는 전송에 쓰이지 않는다(tracing이 꺼져 있다).
"""

import logging

from langfuse import Langfuse, get_client

from src.core.config import settings

logger = logging.getLogger(__name__)

# 비활성 모드에서 싱글턴을 선점할 때 쓰는 자리표시 값. tracing_enabled=False라 전송되지 않는다.
_DISABLED_PLACEHOLDER = "disabled"


def init_langfuse() -> None:
    """앱 시작 시 Langfuse를 초기화한다. 키가 비면 no-op(로컬·CI는 끈다)."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        # 자동 생성이 경고를 찍기 전에 비활성 싱글턴을 선점한다(모듈 docstring 참조).
        Langfuse(
            public_key=_DISABLED_PLACEHOLDER,
            secret_key=_DISABLED_PLACEHOLDER,
            tracing_enabled=False,
        )
        logger.info("LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정 — Langfuse 비활성(no-op)")
        return
    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        # 환경 구분은 Sentry와 같은 값을 쓴다 — 관측 도구 간 환경 이름이 갈리지 않게 한다.
        environment=settings.sentry_environment,
        release=settings.app_version,
    )
    logger.info(
        "Langfuse 활성 — host=%s env=%s", settings.langfuse_host, settings.sentry_environment
    )


def shutdown_langfuse() -> None:
    """앱 종료 시 미전송 트레이스를 밀어낸다.

    Langfuse는 배치 전송이라 flush 없이 프로세스가 죽으면 마지막 구간의 관측이 유실된다.
    컨테이너 재시작·배포마다 공백이 생기지 않도록 lifespan 종료 훅에서 부른다. 비활성
    모드에서도 안전하다(보낼 것이 없다).
    """
    get_client().flush()
