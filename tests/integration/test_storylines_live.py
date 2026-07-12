"""스토리라인 생성 라이브 테스트 (KNK-574 감사 2장 3번).

그동안 storylines는 mock API 테스트만 있어, 실 LLM이 "3편·편당 추천 3개" 계약을
지키는지 관측할 자동 수단이 0이었다(개수 강제는 스키마에 없다 — KNK-578 계약 결정
대상). 전 구간(HTTP → 조립 → 실 LLM → 직렬화)을 ASGI client로 거쳐 실측한다.

주의: 라이브 호출은 과금된다. RUN_LIVE_TESTS=1로 명시 옵트인할 때만 실행된다.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def require_live_env() -> None:
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("RUN_LIVE_TESTS=1이 아니면 라이브 통합 테스트를 건너뜁니다")


async def test_storylines_live(client) -> None:
    payload = {
        "genre_tags": ["다크 판타지"],
        "protagonist_tags": ["신중한", "관찰력 있는"],
        "supporting_tags": ["충직한", "계산적인", "거친"],
    }
    resp = await client.post("/api/v1/story/storylines", json=payload)

    assert resp.status_code == 200
    body = resp.json()

    stories = body["stories"]
    assert len(stories) == 3  # 스토리라인 3편 계약(실 LLM 관측 — 스키마 미강제)
    for s in stories:
        assert isinstance(s["id"], int)
        assert s["storyline"].strip()
        assert len(s["recommended_infos"]) == 3  # 편당 추천 추가정보 3개
        assert all(r.strip() for r in s["recommended_infos"])

    # 로깅 meta(snake_case) — usage 토큰이 실제로 채워지는지 관측.
    meta = body["meta"]
    assert meta["input_token_count"] and meta["input_token_count"] > 0
    assert meta["output_token_count"] and meta["output_token_count"] > 0
