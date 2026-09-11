"""DeepSeek 피크·오프피크 판정 테스트(KNK-1195).

공식 문서의 피크 시간(UTC 월~금 01:00~04:00·06:00~10:00)을 경계·요일·시간대 변환까지 고정한다.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.services.llm import deepseek_pricing as dp

_KST = timezone(timedelta(hours=9))

# 2026-09-09는 수요일, 2026-09-12는 토요일, 2026-09-13은 일요일.
_WED = datetime(2026, 9, 9, tzinfo=UTC)


def _at(hour: int, minute: int = 0, second: int = 0, day: datetime = _WED) -> datetime:
    return day.replace(hour=hour, minute=minute, second=second)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_at(0, 59, 59), "off_peak"),  # 첫 구간 직전
        (_at(1, 0, 0), "peak"),  # 첫 구간 시작(포함)
        (_at(3, 59, 59), "peak"),
        (_at(4, 0, 0), "off_peak"),  # 첫 구간 끝(제외)
        (_at(5, 30), "off_peak"),  # 두 구간 사이
        (_at(6, 0, 0), "peak"),  # 둘째 구간 시작(포함)
        (_at(9, 59, 59), "peak"),
        (_at(10, 0, 0), "off_peak"),  # 둘째 구간 끝(제외)
        (_at(15, 0), "off_peak"),
    ],
)
def test_weekday_peak_windows_are_start_inclusive_end_exclusive(
    moment: datetime, expected: str
) -> None:
    assert dp.pricing_window(moment) == expected


@pytest.mark.parametrize(
    "day",
    [datetime(2026, 9, 12, tzinfo=UTC), datetime(2026, 9, 13, tzinfo=UTC)],
)
def test_weekends_are_always_off_peak(day: datetime) -> None:
    """토·일은 피크 시간대라도 오프피크다(공식 문서: Monday through Friday)."""
    assert dp.pricing_window(_at(2, day=day)) == "off_peak"
    assert dp.pricing_window(_at(8, day=day)) == "off_peak"


def test_other_timezones_are_converted_to_utc() -> None:
    """KST 11시(=UTC 2시)는 피크, KST 20시(=UTC 11시)는 오프피크. 판정은 UTC 기준이다."""
    assert dp.pricing_window(datetime(2026, 9, 9, 11, 0, tzinfo=_KST)) == "peak"
    assert dp.pricing_window(datetime(2026, 9, 9, 20, 0, tzinfo=_KST)) == "off_peak"


def test_naive_datetime_is_treated_as_utc() -> None:
    assert dp.pricing_window(datetime(2026, 9, 9, 7, 0)) == "peak"


def test_kst_friday_night_maps_to_saturday_utc_boundary_correctly() -> None:
    """KST 토요일 10시는 UTC 토요일 1시 — 요일 판정도 UTC로 한 뒤 해야 오프피크가 맞다."""
    assert dp.pricing_window(datetime(2026, 9, 12, 10, 0, tzinfo=_KST)) == "off_peak"


def test_metadata_shape_matches_langfuse_tier_condition() -> None:
    """Langfuse 단가표 구간 조건이 보는 키·값 그대로다. 바꾸면 Langfuse 등록도 바꿔야 한다."""
    assert dp.pricing_metadata(_at(2)) == {"pricing_window": "peak"}
    assert dp.pricing_metadata(_at(12)) == {"pricing_window": "off_peak"}
    assert dp.PRICING_WINDOW_KEY == "pricing_window"


def test_default_uses_current_time() -> None:
    """인자를 안 주면 지금 시각으로 판정한다 — 값 자체는 시각에 따라 다르므로 형태만 본다."""
    assert dp.pricing_window() in {"peak", "off_peak"}
