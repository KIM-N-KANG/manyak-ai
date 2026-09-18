"""KNK-1332: 두 HTTP API의 인원수 전달·응답·실패 계약.

백엔드 대신 같은 입력 목록과 선택한 스토리라인을 다음 요청으로 보낸다.
LLM·이미지 호출은 가짜이며 실제 백엔드 전달이나 서술 품질을 검증하지 않는다.
"""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from src.schemas.story_compile import ThumbnailImageOut
from src.services import story_llm


def _spec(count: int, input_present: bool = True) -> dict:
    path = Path(__file__).parent / "unit/fixtures/spec_valid.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    template = data["prompt_settings"]["character_setting"][0]
    data["prompt_settings"]["character_setting"] = [
        dict(template, name=f"생성인물{i}",
             input_character_id=f"input-{i + 1}" if input_present else None)
        for i in range(count)
    ]
    return data


@pytest.fixture
def image_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    images = AsyncMock(return_value=[])
    thumbnail = AsyncMock(return_value=ThumbnailImageOut(error="generation_failed"))
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", thumbnail)
    return images, thumbnail


@pytest.mark.parametrize("input_count,output_count", [
    (0, 1), (0, 2), (0, 5), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5),
])
@pytest.mark.parametrize("named", [False, True])
async def test_storylines_to_compile_preserves_roster(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
    image_calls: tuple[AsyncMock, AsyncMock],
    input_count: int, output_count: int, named: bool,
) -> None:
    supporting = [
        {"name": f"입력인물{i}"} if named and i % 2 == 0 else {}
        for i in range(input_count)
    ]
    request = {"genre_tags": ["무협"], "protagonist": {},
               "supporting_characters": supporting}
    names = [item["name"] for item in supporting if "name" in item]
    stories = [{"id": i, "storyline": f"{' '.join(names)} 이야기 {i}",
                "recommended_infos": ["정보 하나", "정보 둘", "정보 셋"]}
               for i in range(1, 4)]
    calls: list[tuple[str, str]] = []

    async def complete(system: str, user: str, **kwargs: object) -> tuple[dict, story_llm.LlmUsage]:
        label = str(kwargs["label"])
        calls.append((label, user))
        if label == "storylines":
            data = {"stories": deepcopy(stories)}
        else:
            assert label == "compile", "정상 목록에 불필요한 보완 호출이 발생했다"
            data = _spec(output_count, input_present=input_count > 0)
            data["prompt_settings"]["character_setting"].reverse()
        return data, story_llm.LlmUsage("test", 10, 20, provider="openai")

    monkeypatch.setattr(story_llm, "_complete_json", complete)
    response = await client.post("/api/v1/story/storylines", json=request)
    assert response.status_code == 200
    selected = response.json()["stories"][1]["storyline"]
    response = await client.post("/api/v1/story/compile", json={
        **request, "selected_storyline": selected,
    })
    assert response.status_code == 200
    assert [label for label, _ in calls] == ["storylines", "compile"]
    for _, prompt in calls:
        assert f"입력 주변 인물 수: {input_count}명" in prompt
        assert "{{주변_인물" not in prompt
    assert selected in calls[1][1]
    for i, item in enumerate(supporting):
        name = item.get("name", "(미정)")
        assert f"{i + 1}) 이름: {name}" in calls[0][1]
        assert f"[input_character_id: input-{i + 1}] 이름: {name}" in calls[1][1]

    body = response.json()
    expected = [supporting[i].get("name", f"생성인물{i}")
                if input_count else f"생성인물{i}"
                for i in reversed(range(output_count))]
    assert [item["name"] for item in body["character_appearances"]] == expected
    headings = [line[3:] for line in body["story_settings"]["character_setting"].splitlines()
                if line.startswith("## ")]
    assert headings == expected
    assert "input_character_id" not in response.text
    assert body["meta"]["retry_count"] == 0
    for image_call in image_calls:
        image_call.assert_awaited_once()
        assert [card.name for card in image_call.call_args.args[0]] == expected


@pytest.mark.parametrize("recovery_attempt", [None, 1, 2])
async def test_compile_http_refill_limit_and_image_gate(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
    image_calls: tuple[AsyncMock, AsyncMock], recovery_attempt: int | None,
) -> None:
    """1명 요청에 2명이 나온 경우의 200/502와 마지막 보완 기회를 확인한다."""
    invalid = _spec(2)
    invalid["prompt_settings"]["character_setting"][1]["input_character_id"] = None
    calls: list[str] = []

    async def complete(system: str, user: str, **kwargs: object) -> tuple[dict, story_llm.LlmUsage]:
        calls.append(str(kwargs["label"]))
        attempt = len(calls) - 1
        if attempt == 0:
            data = deepcopy(invalid)
        else:
            assert "입력 주변 인물 수: 1명" in user
            fixed = recovery_attempt is not None and attempt >= recovery_attempt
            data = {"character_setting": _spec(1 if fixed else 2)["prompt_settings"]["character_setting"]}
        return data, story_llm.LlmUsage("test", 10, 20, provider="openai")

    monkeypatch.setattr(story_llm, "_complete_json", complete)
    response = await client.post("/api/v1/story/compile", json={
        "selected_storyline": "선택한 이야기", "genre_tags": ["무협"],
        "protagonist": {}, "supporting_characters": [{}],
    })
    attempts = recovery_attempt if recovery_attempt is not None else 2
    assert calls == ["compile"] + [f"refill#{i}" for i in range(1, attempts + 1)]
    if recovery_attempt is None:
        assert response.status_code == 502
        assert "story_settings" not in response.json()
        for image_call in image_calls:
            image_call.assert_not_awaited()
    else:
        assert response.status_code == 200
        body = response.json()
        assert len(body["character_appearances"]) == 1
        assert body["meta"]["retry_count"] == attempts
        assert body["meta"]["input_token_count"] == 10 * (attempts + 1)
        assert body["meta"]["output_token_count"] == 20 * (attempts + 1)
        assert "input_character_id" not in response.text
        for image_call in image_calls:
            image_call.assert_awaited_once()
            assert len(image_call.call_args.args[0]) == 1
