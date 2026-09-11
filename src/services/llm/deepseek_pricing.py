"""DeepSeek 피크·오프피크 판정 — Langfuse가 시간대별 단가를 고르게 하는 꼬리표(KNK-1195).

DeepSeek은 같은 모델이라도 호출 시각에 따라 단가가 둘이다. 피크 시간은 오프피크의 2배다.
Langfuse 단가표에는 시각 조건이 없고 관측 metadata 값 조건만 있어서, 서버가 호출마다
"지금이 피크인가"를 판단해 metadata로 붙인다. Langfuse에는 기본 구간을 오프피크로, metadata가
피크일 때 적용되는 구간을 따로 등록해 둔다(공식 가격 문서: api-docs.deepseek.com/quick_start/pricing).

피크 시간(공식 문서, 2026-09-11 확인): UTC **월~금** 01:00~04:00·06:00~10:00. 시작은 포함, 끝은 제외.
"""

from datetime import UTC, datetime, time

# Langfuse 단가표의 구간 조건이 보는 metadata 키와 값. 바꾸면 Langfuse 등록도 같이 바꿔야 한다.
PRICING_WINDOW_KEY = "pricing_window"
PRICING_WINDOW_PEAK = "peak"
PRICING_WINDOW_OFF_PEAK = "off_peak"

# (시작 포함, 끝 제외) UTC 시각 구간. 평일에만 적용된다.
_PEAK_WINDOWS_UTC: tuple[tuple[time, time], ...] = (
    (time(1, 0), time(4, 0)),
    (time(6, 0), time(10, 0)),
)
_WEEKDAYS = frozenset(range(5))  # 월(0)~금(4)


def pricing_window(now: datetime | None = None) -> str:
    """지정 시각(기본 현재)이 DeepSeek 피크 시간이면 ``peak``, 아니면 ``off_peak``.

    naive datetime은 UTC로 간주한다. 서버 시계와 DeepSeek 과금 시계의 미세한 차이는 감수한다 —
    경계 몇 초의 오차보다, 비용이 0으로 잡히는 지금 상태를 고치는 것이 목적이다.
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    if moment.weekday() not in _WEEKDAYS:
        return PRICING_WINDOW_OFF_PEAK
    clock = moment.time()
    for start, end in _PEAK_WINDOWS_UTC:
        if start <= clock < end:
            return PRICING_WINDOW_PEAK
    return PRICING_WINDOW_OFF_PEAK


def pricing_metadata(now: datetime | None = None) -> dict[str, str]:
    """Langfuse 관측에 실을 metadata 한 줄. ``{"pricing_window": "peak"|"off_peak"}``."""
    return {PRICING_WINDOW_KEY: pricing_window(now)}
