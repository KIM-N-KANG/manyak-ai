import json
from pathlib import Path

import pytest
from fastapi import HTTPException, status
from httpx import AsyncClient

from src.services import story_llm

_FIXTURES = Path(__file__).parent / "unit" / "fixtures"


def _spec_valid() -> dict:
    return json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8"))


# 정상 요청 본문 — 백엔드가 보내는 희소 입력 형태.
_REQUEST = {
    "selected_storyline": "역병과 반란으로 무너진 왕국에서 견습 기사가 선왕의 의문사를 좇는다.",
    "additional_info": "주인공은 복수보다 진실을 택한다.",
    "genre_tags": ["다크 판타지"],
    "protagonist_tags": ["신중한"],
    "supporting_tags": ["충직한", "거친"],
}


async def test_compile_endpoint_returns_nested_contract(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 경로: LLM을 가짜로 두고 ERD 4테이블 nested 계약이 그대로 내려오는지 확인."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _spec_valid(), story_llm.LlmUsage("deepseek-test", 100, 200)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/compile", json=_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["stories"]["title"] == "잿빛 왕관"
    assert "genre" not in body["stories"]  # genre는 백엔드가 입력 태그로 채움
    assert body["story_settings"]["world_setting"].startswith("# 세계관")
    assert "## 레이" in body["story_settings"]["character_setting"]
    assert len(body["story_suggested_inputs"]) == 3
    # KNK-417: 엔딩·주요 사건이 응답 계약에 실린다(항목별 이산 필드)
    assert 3 <= len(body["story_main_events"]) <= 5
    assert body["story_main_events"][0]["key_sentence"]
    assert sorted(e["ending_type"] for e in body["story_endings"]) == ["BAD", "HAPPY", "NORMAL"]
    assert body["story_endings"][0]["ending_requirement"]
    assert body["story_endings"][0]["ending_epilogue"]
    # 로깅 메타(KNK-243): story는 snake_case 와이어
    meta = body["meta"]
    assert meta["model"] == "deepseek-test"
    assert meta["provider"] == "deepseek"
    assert meta["prompt_versions"]["COMPILE"] >= 1
    assert meta["input_token_count"] == 100
    assert meta["output_token_count"] == 200
    assert meta["retry_count"] == 0
    assert "promptVersions" not in meta  # camelCase 아님(story는 snake)


async def test_compile_endpoint_502_on_llm_error(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 연동 오류는 502로 전파되는지 확인."""

    async def boom(system: str, user: str, **_kwargs: object) -> dict:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="LLM 오류")

    monkeypatch.setattr(story_llm, "_complete_json", boom)

    response = await client.post("/api/v1/story/compile", json=_REQUEST)
    assert response.status_code == 502
