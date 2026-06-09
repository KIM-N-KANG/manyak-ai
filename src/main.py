from fastapi import FastAPI

from src.api.router import api_router
from src.core.config import settings

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
)

app.include_router(api_router)
