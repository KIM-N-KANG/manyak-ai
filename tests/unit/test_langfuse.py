"""Langfuse 관측 초기화·트레이스 묶기 검증(KNK-624·640).

비활성(키 없음) 경로의 안전성과, 활성 경로에서 분석 차원이 어디에 어떤 형태로 실리는지를
가짜 SDK로 검증한다(무과금 — 실제 Langfuse 전송 없음). 활성 경로 검증이 없으면 "차원이
트레이스 태그·관측 metadata에 제대로 붙는가"가 회귀 그물 밖으로 새므로(KNK-640 리뷰) 명시 추가한다.
"""

from contextlib import contextmanager

import langfuse as langfuse_pkg

import src.core.langfuse as lf
from src.core.langfuse import dimension_tags, observe_request, shutdown_langfuse


def test_init_langfuse_noop_without_keys(monkeypatch) -> None:
    # 키가 없으면 활성화되지 않는다(_state.enabled=False) — 계측 import·SDK 생성이 일어나지 않는다.
    monkeypatch.setattr(lf.settings, "langfuse_public_key", "")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "")
    monkeypatch.setattr(lf._state, "enabled", False)
    lf.init_langfuse()
    assert lf._state.enabled is False


def test_init_langfuse_blocked_when_host_not_jp(monkeypatch, caplog) -> None:
    """활성화 가드(KNK-652): 키가 있어도 host가 JP가 아니면 켜지 않는다 — 원문이 다른
    리전으로 새는 것을 코드로 차단(§6-7 예외는 JP 한정). 가드는 계측 import 전에 반환하므로
    SDK를 건드리지 않고, 기동을 막지 않되 오류 로그를 남긴다."""
    monkeypatch.setattr(lf.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(lf.settings, "langfuse_host", "https://cloud.langfuse.com")  # 코드 기본값(JP 아님)
    monkeypatch.setattr(lf.settings, "sentry_environment", "prod")
    monkeypatch.setattr(lf._state, "enabled", False)
    with caplog.at_level("ERROR"):
        lf.init_langfuse()
    assert lf._state.enabled is False
    assert any("JP" in r.message for r in caplog.records)  # 오류 로그로 원인을 알린다


def test_init_langfuse_blocked_when_not_prod(monkeypatch, caplog) -> None:
    """활성화 가드(KNK-652): 키·JP host가 맞아도 환경이 prod가 아니면 켜지 않는다 —
    원문 수집은 prod 전용(§6-7). dev·local에 키가 흘러들어도 조용히 켜지지 않는다."""
    monkeypatch.setattr(lf.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(lf.settings, "langfuse_host", "https://jp.cloud.langfuse.com")
    monkeypatch.setattr(lf.settings, "sentry_environment", "local")
    monkeypatch.setattr(lf._state, "enabled", False)
    with caplog.at_level("ERROR"):
        lf.init_langfuse()
    assert lf._state.enabled is False
    assert any("prod" in r.message for r in caplog.records)


def test_init_langfuse_enabled_with_jp_prod_and_keys(monkeypatch) -> None:
    """활성화 정상 경로(KNK-652 적대 리뷰): 키 + JP host + prod 환경 → 켜진다.

    차단 테스트만 있으면 허용값 상수의 오타(예: "prod"→"production")가 스위트를 전부
    통과한 채 프로덕션 활성화를 조용히 죽인다 — 켜지는 쪽을 고정해 그 변이를 잡는다.
    실제 계측 import의 부작용(openai 전역 몽키패치)이 다른 테스트로 새지 않도록
    `langfuse.openai`는 빈 가짜 모듈로, SDK 생성자는 기록용 가짜로 바꾼다(무과금)."""
    import sys
    import types

    monkeypatch.setattr(lf.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "sk-test")
    # 후행 슬래시 변형도 정규화로 통과해야 한다 — 가드 비교·SDK 전달이 같은 값(host 비대칭 방지).
    monkeypatch.setattr(lf.settings, "langfuse_host", "https://jp.cloud.langfuse.com/")
    monkeypatch.setattr(lf.settings, "sentry_environment", "prod")
    monkeypatch.setattr(lf._state, "enabled", False)

    captured: dict = {}

    class _FakeLangfuse:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langfuse_pkg, "Langfuse", _FakeLangfuse)
    monkeypatch.setitem(sys.modules, "langfuse.openai", types.ModuleType("langfuse.openai"))

    lf.init_langfuse()
    assert lf._state.enabled is True
    assert captured["host"] == "https://jp.cloud.langfuse.com"  # 정규화된 값이 SDK로 간다
    assert captured["environment"] == "prod"


def test_observe_request_passthrough_when_disabled(monkeypatch) -> None:
    # 비활성이면 observe_request는 Langfuse SDK를 건드리지 않고 블록을 그대로 통과시킨다.
    # 차원 인자(tags·metadata)를 줘도 no-op이어야 하고, 핸들의 set_metadata도 안전해야 한다.
    monkeypatch.setattr(lf._state, "enabled", False)
    ran = False
    with observe_request(
        "테스트", tags=["genre:무협"], metadata={"prompt_versions": {"CORE": 1}}
    ) as trace:
        ran = True
        trace.set_metadata(retry_count=2)  # 비활성 핸들 — 예외 없이 통과해야 한다
    assert ran is True


def test_shutdown_langfuse_safe_when_disabled(monkeypatch) -> None:
    # 비활성이면 flush 대상이 없다 — SDK를 import조차 하지 않으므로 예외 없이 반환해야 한다.
    monkeypatch.setattr(lf._state, "enabled", False)
    shutdown_langfuse()  # 예외가 나면 실패


def test_dimension_tags_genre_only() -> None:
    # 장르만 태그로 만든다 — 주인공·조연(자유입력 섞임)은 인자 자체가 없어 새어 나갈 경로가 없다.
    # 채팅용 단일 genre 인자는 KNK-652에서 제거됨(장르 태그는 스토리 제작 트레이스에만).
    assert dimension_tags(genre_tags=["무협", "복수극"]) == ["genre:무협", "genre:복수극"]
    assert dimension_tags() == []  # 아무것도 안 주면 빈 리스트


def test_dimension_tags_accepts_only_genre_tags() -> None:
    """dimension_tags의 인자를 genre_tags 하나로 고정(KNK-652 회귀 방지) — 채팅용 genre가
    같은 이름이든 다른 이름(chat_genre 등)이든 새 인자가 생기면 실패한다. 장르 태그는
    스토리 제작 트레이스에만 싣는다(5-ai-server §5-6)."""
    import inspect

    assert list(inspect.signature(dimension_tags).parameters) == ["genre_tags"]


class _FakeSpan:
    """가짜 루트 스팬 — update(metadata=) 호출을 기록한다."""

    def __init__(self) -> None:
        self.metadata: dict = {}

    def update(self, *, metadata: dict) -> None:
        self.metadata.update(metadata)


def test_observe_request_active_attaches_dimensions(monkeypatch) -> None:
    """활성 경로: tags는 propagate_attributes로, metadata(버전·request_id·retry_count)는
    루트 관측에 1회 기록되는지 가짜 SDK로 확인한다(무과금)."""
    monkeypatch.setattr(lf._state, "enabled", True)
    monkeypatch.setattr(lf, "get_correlation_ids", lambda: ("req-1", "sess-1", "hash-1"))

    fake_span = _FakeSpan()
    captured: dict = {}

    @contextmanager
    def fake_current_observation(*, name, as_type):
        captured["name"] = name
        yield fake_span

    @contextmanager
    def fake_propagate(**kwargs):
        captured["propagated"] = kwargs
        yield

    class _FakeClient:
        def start_as_current_observation(self, *, name, as_type):
            return fake_current_observation(name=name, as_type=as_type)

    # observe_request가 함수 안에서 `from langfuse import ...` 하므로 langfuse 패키지를 패치한다.
    monkeypatch.setattr(langfuse_pkg, "get_client", lambda: _FakeClient(), raising=False)
    monkeypatch.setattr(langfuse_pkg, "propagate_attributes", fake_propagate, raising=False)

    # 픽스처는 스토리 컴파일로 — 장르 태그를 싣는 트레이스는 스토리 제작뿐이다(KNK-652).
    with observe_request(
        "스토리 컴파일",
        tags=["genre:판타지"],
        metadata={"prompt_versions": {"CORE": 3}, "retry_count": 0},
    ) as trace:
        trace.set_metadata(retry_count=2)  # 사후 값이 같은 버퍼에 병합돼야 한다

    # 정체성은 propagate로 — session/user/tags/trace_name
    assert captured["propagated"]["session_id"] == "sess-1"
    assert captured["propagated"]["user_id"] == "hash-1"
    assert captured["propagated"]["tags"] == ["genre:판타지"]
    assert captured["propagated"]["trace_name"] == "스토리 컴파일"
    # 분석 metadata는 루트 관측에 1회로 모여 기록 — 사후 retry_count(2)가 미리값(0)을 덮는다
    assert fake_span.metadata == {
        "request_id": "req-1",
        "prompt_versions": {"CORE": 3},
        "retry_count": 2,
    }


def test_observe_request_active_survives_metadata_failure(monkeypatch) -> None:
    """관측 기록(span.update)이 실패해도 블록은 정상 종료 — 관측이 서비스를 깨지 않는다."""
    monkeypatch.setattr(lf._state, "enabled", True)
    monkeypatch.setattr(lf, "get_correlation_ids", lambda: (None, None, None))

    class _BoomSpan:
        def update(self, *, metadata: dict) -> None:
            raise RuntimeError("langfuse 다운")

    @contextmanager
    def fake_current_observation(*, name, as_type):
        yield _BoomSpan()

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    class _FakeClient:
        def start_as_current_observation(self, *, name, as_type):
            return fake_current_observation(name=name, as_type=as_type)

    monkeypatch.setattr(langfuse_pkg, "get_client", lambda: _FakeClient(), raising=False)
    monkeypatch.setattr(langfuse_pkg, "propagate_attributes", fake_propagate, raising=False)

    ran = False
    with observe_request("채팅 턴", metadata={"retry_count": 0}) as trace:
        ran = True
        trace.set_metadata(retry_count=1)
    assert ran is True  # span.update가 던져도 예외가 밖으로 새지 않는다
