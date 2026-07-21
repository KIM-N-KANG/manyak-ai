"""Langfuse 관측 초기화·트레이스 묶기 — 계측이 켜지지 않은 경로의 안전성 검증(KNK-624).

핵심 계약은 "키가 없으면 계측 자체가 실리지 않는다"이다. 이 계약이 깨지면 experiment 러너·
scripts 로컬 도구가 원치 않게 계측을 물려받거나(전역 monkey-patch), 테스트·CI가 외부 Langfuse로
트레이스를 보낸다. 실제 SDK 전송은 하지 않고(무과금), 활성 여부와 no-op 통과만 확인한다.
"""

import src.core.langfuse as lf
from src.core.langfuse import observe_request, shutdown_langfuse


def test_init_langfuse_noop_without_keys(monkeypatch) -> None:
    # 키가 없으면 활성화되지 않는다(_state.enabled=False) — 계측 import·SDK 생성이 일어나지 않는다.
    monkeypatch.setattr(lf.settings, "langfuse_public_key", "")
    monkeypatch.setattr(lf.settings, "langfuse_secret_key", "")
    monkeypatch.setattr(lf._state, "enabled", False)
    lf.init_langfuse()
    assert lf._state.enabled is False


def test_observe_request_passthrough_when_disabled(monkeypatch) -> None:
    # 비활성이면 observe_request는 Langfuse SDK를 건드리지 않고 블록을 그대로 통과시킨다.
    monkeypatch.setattr(lf._state, "enabled", False)
    ran = False
    with observe_request("테스트"):
        ran = True
    assert ran is True


def test_shutdown_langfuse_safe_when_disabled(monkeypatch) -> None:
    # 비활성이면 flush 대상이 없다 — SDK를 import조차 하지 않으므로 예외 없이 반환해야 한다.
    monkeypatch.setattr(lf._state, "enabled", False)
    shutdown_langfuse()  # 예외가 나면 실패
