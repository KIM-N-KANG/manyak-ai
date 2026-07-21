from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.router import api_router
from src.core.config import settings
from src.core.langfuse import init_langfuse, shutdown_langfuse
from src.core.middleware import RequestContextMiddleware
from src.core.sentry import init_sentry

init_sentry()  # 앱 생성 전에 Sentry를 켠다(DSN 미설정 시 no-op).
init_langfuse()  # Langfuse도 앱 생성 전에 켠다(키 미설정·JP·prod 미충족 시 no-op) — KNK-624·652.


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
