import pytest
from httpx import AsyncClient

from src.services import story_llm

# storylines 엔드포인트의 정상 요청 본문(백엔드가 보내는 태그 3종).
_REQUEST = {
    "genre_tags": ["무협", "생존"],
    "protagonist_tags": ["천마신교", "계획적인"],
    "supporting_tags": ["다정한", "정파"],
}

# storylines 출력 스키마({"stories":[{id, storyline, recommended_infos}]})를 흉내 낸 가짜 결과.
_FAKE = {
    "stories": [
        {"id": 1, "storyline": "스토리 1", "recommended_infos": ["가", "나", "다"]},
        {"id": 2, "storyline": "스토리 2", "recommended_infos": ["가", "나", "다"]},
        {"id": 3, "storyline": "스토리 3", "recommended_infos": ["가", "나", "다"]},
    ]
}


async def test_storylines_endpoint_attaches_meta(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 경로: 응답에 로깅 메타(snake_case)가 붙고 prompt_versions 키가 STORYLINES인지 확인."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", 50, 80)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert [s["id"] for s in body["stories"]] == [1, 2, 3]

    # 로깅 메타(KNK-243): story는 snake_case 와이어, 재호출이 없었으면 retry_count=0
    meta = body["meta"]
    assert meta["model"] == "deepseek-test"
    assert meta["provider"] == "deepseek"
    assert list(meta["prompt_versions"]) == ["STORYLINES"]
    assert meta["prompt_versions"]["STORYLINES"] >= 1
    assert meta["input_token_count"] == 50
    assert meta["output_token_count"] == 80
    assert meta["retry_count"] == 0
    assert "promptVersions" not in meta  # camelCase 아님(story는 snake)


async def test_storylines_endpoint_serializes_missing_tokens_as_null(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """공급자가 토큰을 안 줬을 때 응답 본문에 **null**로 나가는지 HTTP 계층까지 확인한다.

    백엔드 계약이 "누락 시 null"(0이 아니다)이다. `_complete_json`이 None을 들고 오는 것만
    확인하면 응답 조립·직렬화가 None을 거부하도록 망가져도 안 잡힌다(KNK-672 리뷰).
    """

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", None, None)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["input_token_count"] is None  # 0으로 뭉개지 않는다
    assert meta["output_token_count"] is None


async def test_storylines_endpoint_tolerates_meta_key_in_llm_result(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM이 변덕으로 'meta' 키를 섞어 보내도 kwarg 충돌(500) 없이 정상 응답해야 한다."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return {**_FAKE, "meta": "LLM이 섞어 보낸 잡음"}, story_llm.LlmUsage("m", 1, 2)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    # 서버가 만든 메타로 덮어써지고, LLM이 보낸 잡음 문자열은 채택되지 않는다.
    assert response.json()["meta"]["provider"] == "deepseek"


async def test_storylines_endpoint_reports_actual_retry_count(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재호출이 있었으면 meta.retry_count가 하드코딩 0이 아니라 실제 횟수를 싣는다(KNK-312)."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", 100, 160, retry_count=1)

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["retry_count"] == 1
    # 합산 토큰이 그대로 실린다(실패 시도분 포함 값)
    assert meta["input_token_count"] == 100
    assert meta["output_token_count"] == 160
