import os

# F3(KNK-266 리뷰): 테스트는 실제 Sentry로 이벤트를 보내지 않는다. SENTRY_DSN을 비워(.env 값을
# 무시) init_sentry()를 no-op으로 만든다 — src.main import(=init 호출 시점)보다 먼저 설정해야 한다.
os.environ["SENTRY_DSN"] = ""

import pytest  # noqa: E402
import sentry_sdk  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from src.main import app  # noqa: E402


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_sentry_scope():
    """매 테스트를 새 isolation scope로 격리해 미들웨어가 심은 tag가 테스트 간 누수되지 않게 한다(KNK-266)."""
    with sentry_sdk.isolation_scope():
        yield
