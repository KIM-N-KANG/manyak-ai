import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.router import api_router
from src.core.config import settings
from src.core.langfuse import init_langfuse, shutdown_langfuse
from src.core.middleware import RequestContextMiddleware
from src.core.sentry import init_sentry
from src.services.llm import validate_startup

# 컨테이너(uvicorn)는 루트 로거에 핸들러를 달지 않아 앱 INFO 로그가 통째로 버려진다
# (ERROR만 lastResort로 보임). "Langfuse 활성/비활성" 상태 로그는 CI 스모크와 배포 후
# 점검(7-deployment §7-9)의 근거라 보여야 한다(Codex P1). basicConfig는 루트에 핸들러가
# 이미 있으면 no-op이라 pytest·로컬 실행과 충돌하지 않는다.
logging.basicConfig(level=logging.INFO)

init_sentry()  # 앱 생성 전에 Sentry를 켠다(DSN 미설정 시 no-op).
init_langfuse()  # Langfuse도 앱 생성 전에 켠다(키 미설정·JP·prod 미충족 시 no-op) — KNK-624·652.
# 선택된 모델(STORYLINES_MODEL·STORY_COMPILE_MODEL·CHAT_MODEL)이 등록부에 있고, 공급자 키가
# 채워졌고, 담당 어댑터가 그 설정을 표현할 수 있는지 기동에서 확인한다(KNK-670·671).
# 여기서 막히면 배포가 실패하고 AI 서버가 내려간다(배포는 기존 컨테이너를 교체한다 —
# manyak-terraform의 `docker compose up -d --wait ai`). 그래도 이쪽이 낫다: 잘못된 설정으로
# 뜬 서버는 요청마다 500·502를 내면서도 살아 있는 것처럼 보여 원인을 훨씬 늦게 알게 된다.
validate_startup()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """앱 수명주기 훅 — 종료 시 Langfuse의 미전송 트레이스를 밀어낸다.

    Langfuse는 배치 전송이라 flush 없이 프로세스가 죽으면 마지막 구간의 관측이 유실된다.
    시작 쪽은 모듈 로드 시점(init_langfuse)이 이미 처리하므로 여기서는 종료만 맡는다.
    """
    yield
    shutdown_langfuse()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

# 백엔드가 헤더로 넘긴 요청 상관관계 식별자를 요청 단위 context로 옮긴다(KNK-266).
app.add_middleware(RequestContextMiddleware)

app.include_router(api_router)
