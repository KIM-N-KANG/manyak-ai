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

    # 로깅 메타(KNK-243): story는 snake_case 와이어, retry 없음(단일 호출)
    meta = body["meta"]
    assert meta["model"] == "deepseek-test"
    assert meta["provider"] == "deepseek"
    assert list(meta["prompt_versions"]) == ["STORYLINES"]
    assert meta["prompt_versions"]["STORYLINES"] >= 1
    assert meta["input_token_count"] == 50
    assert meta["output_token_count"] == 80
    assert meta["retry_count"] == 0
    assert "promptVersions" not in meta  # camelCase 아님(story는 snake)


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
