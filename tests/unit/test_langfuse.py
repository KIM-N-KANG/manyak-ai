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


def test_init_langfuse_survives_sdk_failure(monkeypatch, caplog) -> None:
    """초기화 보호(P1 리뷰): SDK 생성자가 예외를 내도 앱 기동이 죽지 않고 no-op으로 넘어간다.

    init_langfuse는 main.py 모듈 로드 시점에 불리므로, 여기가 뚫려 있으면 관측 도구
    고장이 서비스 부팅 실패가 된다 — 오류 로그 + enabled=False로 격리돼야 한다.
    또한 계측 import(openai 전역 몽키패치)는 클라이언트 생성 성공 뒤에만 걸리므로(Codex P2),
    생성자가 실패하면 패치가 설치되지 않은 채로 남아야 한다."""
    import sys

    monkeypatch.setattr(lf.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(lf.settings, "langfuse_host", "https://jp.cloud.langfuse.com")
    monkeypatch.setattr(lf.settings, "sentry_environment", "prod")
    monkeypatch.setattr(lf._state, "enabled", False)

    class _BoomLangfuse:
        def __init__(self, **kwargs) -> None:
            raise RuntimeError("SDK 초기화 실패 시뮬레이션")

    monkeypatch.setattr(langfuse_pkg, "Langfuse", _BoomLangfuse)

    with caplog.at_level("ERROR"):
        lf.init_langfuse()  # 예외가 새면 실패
    assert lf._state.enabled is False
    assert any("초기화 실패" in r.message for r in caplog.records)
    # 실패 경로에서는 계측 패치(langfuse.openai import)가 걸리지 않아야 한다(Codex P2).
    assert "langfuse.openai" not in sys.modules


def test_init_failure_after_client_leaves_no_instrumentation(monkeypatch, caplog) -> None:
    """초기화 순서 보호(후속 P2): 클라이언트 생성 **뒤** 단계(ignore_logger)가 실패해도
    계측 패치가 설치되지 않은 깨끗한 비활성으로 남아야 한다.

    계측 import는 되돌릴 수 없으므로 실패 가능한 단계들 전부보다 뒤(맨 마지막)여야 한다 —
    순서가 뒤집히면 "비활성" 로그를 찍고도 LLM 호출이 Langfuse 래퍼를 계속 지나는,
    상태 표시와 실제 동작이 어긋난 상태가 된다(리뷰 재현: enabled=False + 계측 모듈 로드됨)."""
    import sys

    import sentry_sdk.integrations.logging as sentry_logging

    monkeypatch.setattr(lf.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(lf.settings, "langfuse_host", "https://jp.cloud.langfuse.com")
    monkeypatch.setattr(lf.settings, "sentry_environment", "prod")
    monkeypatch.setattr(lf._state, "enabled", False)

    class _FakeLangfuse:
        def __init__(self, **kwargs) -> None:
            pass  # 생성은 성공 — 실패 지점은 그 뒤의 ignore_logger다

    def _boom(name: str) -> None:
        raise RuntimeError("ignore_logger 실패 시뮬레이션")

    monkeypatch.setattr(langfuse_pkg, "Langfuse", _FakeLangfuse)
    monkeypatch.setattr(sentry_logging, "ignore_logger", _boom)

    with caplog.at_level("ERROR"):
        lf.init_langfuse()  # 예외가 새면 실패
    assert lf._state.enabled is False
    assert any("초기화 실패" in r.message for r in caplog.records)
    # 핵심: 실패가 계측 import보다 앞서 일어나므로 패치가 설치되지 않아야 한다.
    assert "langfuse.openai" not in sys.modules


def test_observe_request_survives_trace_start_failure(monkeypatch, caplog) -> None:
    """트레이스 시작 보호(P1 리뷰): get_client 등 SDK 시작 호출이 예외를 내도 본작업은
    관측 없이 진행된다 — 이 블록은 모든 엔드포인트에서 LLM 호출보다 먼저 돌기 때문에,
    보호가 없으면 관측 도구 고장이 모든 요청의 500이 된다(리뷰 재현 시나리오 고정)."""
    monkeypatch.setattr(lf._state, "enabled", True)

    def _boom():
        raise RuntimeError("simulated Langfuse client failure")

    monkeypatch.setattr(langfuse_pkg, "get_client", _boom, raising=False)

    ran = False
    with caplog.at_level("WARNING"):
        with observe_request("채팅 턴", metadata={"retry_count": 0}) as trace:
            ran = True
            trace.set_metadata(retry_count=1)  # 빈 핸들 — 예외 없이 통과해야 한다
    assert ran is True
    assert any("트레이스 시작 실패" in r.message for r in caplog.records)


def test_shutdown_langfuse_survives_flush_failure(monkeypatch, caplog) -> None:
    """종료 flush 보호(P1 리뷰): flush가 예외를 내도 종료 절차가 깨지지 않는다."""
    monkeypatch.setattr(lf._state, "enabled", True)

    class _BoomClient:
        def flush(self) -> None:
            raise RuntimeError("flush 실패 시뮬레이션")

    monkeypatch.setattr(langfuse_pkg, "get_client", lambda: _BoomClient(), raising=False)

    with caplog.at_level("WARNING"):
        shutdown_langfuse()  # 예외가 새면 실패
    assert any("flush 실패" in r.message for r in caplog.records)


def test_observe_request_propagates_business_exception(monkeypatch) -> None:
    """보호의 경계 고정(P1 리뷰): SDK 실패는 삼키되 **본작업이 낸 예외는 그대로 전파**된다 —
    관측 보호가 실제 오류(LLM·비즈니스 예외)까지 숨기면 안 된다."""
    monkeypatch.setattr(lf._state, "enabled", True)
    monkeypatch.setattr(lf, "get_correlation_ids", lambda: (None, None, None))

    @contextmanager
    def fake_current_observation(*, name, as_type):
        yield _FakeSpan()

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    class _FakeClient:
        def start_as_current_observation(self, *, name, as_type):
            return fake_current_observation(name=name, as_type=as_type)

    monkeypatch.setattr(langfuse_pkg, "get_client", lambda: _FakeClient(), raising=False)
    monkeypatch.setattr(langfuse_pkg, "propagate_attributes", fake_propagate, raising=False)

    import pytest

    with pytest.raises(ValueError, match="본작업 실패"):
        with observe_request("스토리 컴파일"):
            raise ValueError("본작업 실패")


def test_teardown_failure_log_does_not_leak_business_exception(monkeypatch, caplog) -> None:
    """원문 유출 차단(Codex P2): 본작업 예외 진행 중 SDK 종료·metadata 기록이 실패해도,
    경고 로그에 본작업 예외 원문이 딸려 나오지 않는다 — 파이썬 예외 연쇄(exc_info) 대신
    예외 타입 이름만 남긴다. 본작업 예외 메시지에는 사용자 입력·LLM 응답 조각이 섞일 수
    있어(AN-4-10 원문 비수집) 로그가 원문 유출 통로가 되면 안 된다."""
    monkeypatch.setattr(lf._state, "enabled", True)
    monkeypatch.setattr(lf, "get_correlation_ids", lambda: (None, None, None))

    class _BoomUpdateSpan:
        def update(self, *, metadata: dict) -> None:
            raise RuntimeError("SDK update failure")

    @contextmanager
    def bad_span(*, name, as_type):
        try:
            yield _BoomUpdateSpan()
        finally:
            raise RuntimeError("SDK exit failure")  # 종료 시에도 SDK 실패

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    class _FakeClient:
        def start_as_current_observation(self, *, name, as_type):
            return bad_span(name=name, as_type=as_type)

    monkeypatch.setattr(langfuse_pkg, "get_client", lambda: _FakeClient(), raising=False)
    monkeypatch.setattr(langfuse_pkg, "propagate_attributes", fake_propagate, raising=False)

    import pytest

    secret = "사용자입력-원문-마커"
    with caplog.at_level("WARNING"):
        with pytest.raises(ValueError, match="본작업"):
            with observe_request("채팅 턴", metadata={"retry_count": 0}) as trace:
                trace.set_metadata(retry_count=1)
                raise ValueError(f"본작업 실패: {secret}")

    assert "기록 실패" in caplog.text or "종료 실패" in caplog.text  # 실패 자체는 로그로 감지
    assert secret not in caplog.text  # 본작업 원문은 로그 어디에도 없다


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
