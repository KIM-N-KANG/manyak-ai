from fastapi import FastAPI

from src.api.router import api_router
from src.core.config import settings
from src.core.sentry import init_sentry

init_sentry()  # 앱 생성 전에 Sentry를 켠다(DSN 미설정 시 no-op).

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
)

app.include_router(api_router)
