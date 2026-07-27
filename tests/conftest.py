import os

# F3(KNK-266 리뷰): 테스트는 실제 Sentry로 이벤트를 보내지 않는다. SENTRY_DSN을 비워(.env 값을
# 무시) init_sentry()를 no-op으로 만든다 — src.main import(=init 호출 시점)보다 먼저 설정해야 한다.
os.environ["SENTRY_DSN"] = ""
# 같은 이유로 Langfuse도 끈다(KNK-624). .env에 키가 있으면 init_langfuse()가 활성화돼 테스트
# 트레이스가 실제 Langfuse 프로젝트로 나간다 — 키를 비워 no-op으로 만든다(SENTRY_DSN과 동일 선례).
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
import sentry_sdk  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from src.main import app  # noqa: E402
from src.services.llm import openai_sdk, registry  # noqa: E402
from src.services.llm.base import ADAPTER_OPENAI_SDK, ResolvedModel  # noqa: E402

# DeepSeek이 아닌 시험용 모델. 모든 테스트가 DeepSeek이면 "모델을 보고 공급자를 정하는 코드"와
# "그냥 'deepseek'이라 적어둔 코드"가 구분되지 않는다(KNK-674).
OTHER_PROVIDER_MODEL = "not-deepseek-model"
OTHER_PROVIDER = "not-deepseek"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_sentry_scope():
    """매 테스트를 새 isolation scope로 격리해 미들웨어가 심은 tag가 테스트 간 누수되지 않게 한다(KNK-266)."""
    with sentry_sdk.isolation_scope():
        yield


class FakeStream:
    """실제 `openai.AsyncStream`의 모양을 흉내 낸다 — 반복 가능하고 `close()`로 닫힌다.

    async generator로 대신하면 `close()`가 없어(그쪽은 `aclose()`) 어댑터의 스트림 정리
    경로가 매번 실패로 빠진다. 목이 실제 타입과 다르면 통과해도 아무것도 증명하지 못한다.
    """

    def __init__(self, chunks: list, error: BaseException | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closed = False

    def __aiter__(self) -> "FakeStream":
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            if self._error is not None:
                raise self._error
            raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def install_llm_sdk(monkeypatch):
    """SDK 경계에 가짜 클라이언트를 심는다(KNK-672·673 이관 후의 공용 목 지점).

    통로(`src.services.llm`)나 호출부를 가로채면 모델 등록부·인자 조립·응답 해석·예외 번역이
    통째로 건너뛰어져, 정작 이관에서 깨지기 쉬운 부분을 검증하지 못한다. 그래서 어댑터가 쓰는
    클라이언트 자체를 바꾼다.
    """

    def _install(create) -> None:
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(openai_sdk, "_client", lambda provider: client)

    return _install


@pytest.fixture
def other_provider_model(monkeypatch):
    """DeepSeek이 아닌 모델을 등록부에 임시로 끼운다(KNK-674 2차 리뷰).

    등록부는 비공개 dict(`registry._REGISTRY`)라 밖에서 모델을 넣을 공식 수단이 없다. 그
    우회 코드가 테스트 9곳에 그대로 복사돼 있어, `ResolvedModel`에 필드가 하나 늘면 9곳을
    함께 고쳐야 했다 — 여기 한 곳으로 모은다.

    모듈을 넘기면 그 모듈이 보는 `settings.chat_model`까지 이 모델로 바꾼다(채팅 3기능).
    스토리는 모델 이름을 인자로 직접 받으므로 등록만 하면 된다.
    """

    def _use(module=None) -> str:
        monkeypatch.setitem(
            registry._REGISTRY,
            OTHER_PROVIDER_MODEL,
            ResolvedModel(
                model=OTHER_PROVIDER_MODEL,
                provider=OTHER_PROVIDER,
                adapter=ADAPTER_OPENAI_SDK,
                use_thinking=True,
            ),
        )
        if module is not None:
            monkeypatch.setattr(module.settings, "chat_model", OTHER_PROVIDER_MODEL)
        return OTHER_PROVIDER_MODEL

    return _use
