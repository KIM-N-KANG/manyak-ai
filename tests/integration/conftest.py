"""라이브 테스트의 SDK 연결 수명을 테스트별 이벤트 루프에 맞춘다."""

from collections.abc import AsyncIterator

import pytest

from src.services.image import openai_api
from src.services.llm import openai_sdk


@pytest.fixture(autouse=True)
async def close_openai_clients() -> AsyncIterator[None]:
    """다음 테스트가 이미 닫힌 루프의 HTTP 연결을 재사용하지 않게 한다."""
    try:
        yield
    finally:
        for cache in (openai_sdk._clients, openai_api._clients):
            clients = list(cache.values())
            cache.clear()
            for client in clients:
                await client.close()
