import sentry_sdk
from httpx import ASGITransport, AsyncClient

from src.api.v1 import story as story_module
from src.core.request_context import (
    clean_identifier,
    get_correlation_ids,
    set_correlation_ids,
)
from src.main import app


class _FakeScope:
    """set_tag/set_context만 기록하는 가짜 isolation scope."""

    def __init__(self) -> None:
        self.tags: dict = {}
        self.contexts: dict = {}

    def set_tag(self, k: str, v: object) -> None:
        self.tags[k] = v

    def set_context(self, k: str, v: object) -> None:
        self.contexts[k] = v


# ── clean_identifier (단위) ─────────────────────────────────────────────────
def test_clean_identifier_passes_value() -> None:
    assert clean_identifier("req_1") == "req_1"


def test_clean_identifier_drops_empty_and_none() -> None:
    assert clean_identifier(None) is None
    assert clean_identifier("") is None


def test_clean_identifier_drops_unknown_sentinel() -> None:
    # 백엔드 필터가 헤더 누락 시 채우는 "unknown"은 버린다(백엔드 SentryMdcEventProcessor와 동일).
    assert clean_identifier("unknown") is None


# ── 미들웨어: 헤더 → 요청별 Sentry isolation scope ───────────────────────────
async def test_middleware_sets_isolation_scope_from_headers(client, monkeypatch) -> None:
    fake = _FakeScope()
    monkeypatch.setattr(sentry_sdk, "get_isolation_scope", lambda: fake)
    resp = await client.get(
        "/api/v1/health",
        headers={
            "X-Manyak-Request-Id": "req_abc",
            "X-Manyak-Session-Id": "sess_1",
            "X-Manyak-Device-Id-Hash": "device_hash_1",
        },
    )
    assert resp.status_code == 200
    # 백엔드 SentryMdcEventProcessor와 동일: request_id는 tag, 나머지는 identity context
    assert fake.tags["request_id"] == "req_abc"
    assert fake.contexts["identity"] == {
        "session_id": "sess_1",
        "device_id_hash": "device_hash_1",
    }


async def test_middleware_no_headers_sets_nothing(client, monkeypatch) -> None:
    fake = _FakeScope()
    monkeypatch.setattr(sentry_sdk, "get_isolation_scope", lambda: fake)
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert fake.tags == {}  # forward-compatible — 헤더 없으면 아무것도 안 싣는다
    assert fake.contexts == {}


async def test_middleware_partial_and_unknown(client, monkeypatch) -> None:
    fake = _FakeScope()
    monkeypatch.setattr(sentry_sdk, "get_isolation_scope", lambda: fake)
    resp = await client.get(
        "/api/v1/health",
        headers={
            "X-Manyak-Request-Id": "req_only",
            "X-Manyak-Session-Id": "unknown",  # sentinel → 버림
        },
    )
    assert resp.status_code == 200
    assert fake.tags["request_id"] == "req_only"
    assert "identity" not in fake.contexts  # session "unknown" 버려 identity 비어 미부착


async def test_middleware_sets_scope_even_on_unhandled_500(monkeypatch) -> None:
    # F1 증거: 미처리 500이 나도, 미들웨어가 진입 시 isolation scope에 request_id를 이미 심는다 →
    # 미들웨어 바깥(ServerErrorMiddleware)의 자동 캡처가 그 scope를 읽어 request_id가 붙는다.
    fake = _FakeScope()
    monkeypatch.setattr(sentry_sdk, "get_isolation_scope", lambda: fake)

    async def _boom(*_a, **_k):
        raise RuntimeError("의도된 미처리 오류")

    monkeypatch.setattr(story_module.story_llm, "generate_storylines", _boom)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        resp = await c.post(
            "/api/v1/story/storylines",
            json={
                "genre_tags": ["무협"],
                "protagonist_tags": ["천마신교"],
                "supporting_tags": ["다정한"],
            },
            headers={"X-Manyak-Request-Id": "req_500"},
        )
    assert resp.status_code == 500
    assert fake.tags["request_id"] == "req_500"  # 500에도 request_id가 scope에 살아 있다


# ── contextvar 갈래: 미들웨어 → 엔드포인트가 읽는 상관관계 식별자(KNK-624) ──────
def test_correlation_contextvar_roundtrip() -> None:
    set_correlation_ids("req_1", "sess_1", "hash_1")
    assert get_correlation_ids() == ("req_1", "sess_1", "hash_1")


def test_correlation_contextvar_overwrites_with_none() -> None:
    # set이 항상 세 값을 덮어써야 이전 요청 값이 재사용 컨텍스트로 새지 않는다.
    set_correlation_ids("req_1", "sess_1", "hash_1")
    set_correlation_ids(None, None, None)
    assert get_correlation_ids() == (None, None, None)


async def test_middleware_populates_correlation_contextvar(client, monkeypatch) -> None:
    # 미들웨어가 Sentry scope와 별개로 contextvar에도 실어, 엔드포인트가 읽을 수 있어야 한다.
    monkeypatch.setattr(sentry_sdk, "get_isolation_scope", lambda: _FakeScope())
    seen: dict = {}

    async def _spy(*_a, **_k):
        seen["ids"] = get_correlation_ids()
        raise RuntimeError("stop")  # LLM 호출 전에 멈춘다(무과금)

    monkeypatch.setattr(story_module.story_llm, "generate_storylines", _spy)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as c:
        await c.post(
            "/api/v1/story/storylines",
            json={"genre_tags": ["무협"], "protagonist_tags": ["천마"], "supporting_tags": ["x"]},
            headers={
                "X-Manyak-Request-Id": "req_ctx",
                "X-Manyak-Session-Id": "sess_ctx",
                "X-Manyak-Device-Id-Hash": "hash_ctx",
            },
        )
    assert seen["ids"] == ("req_ctx", "sess_ctx", "hash_ctx")
