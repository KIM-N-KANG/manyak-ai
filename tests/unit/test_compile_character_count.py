"""KNK-1331: 입력 인물과 컴파일 카드의 일대일 대응 및 보완 한도."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.schemas.story import CharacterInput
from src.schemas.story_compile import StoryCompileRequest
from src.services import story_llm
from src.services.prompt import build_compile_prompt, build_refill_prompt


def _spec(count: int) -> dict:
    data = json.loads((Path(__file__).parent / "fixtures/spec_valid.json").read_text())
    template = data["prompt_settings"]["character_setting"][0]
    data["prompt_settings"]["character_setting"] = [
        dict(template, input_character_id=f"input-{i + 1}", name=f"인물{i + 1}")
        for i in range(count)
    ]
    return data


def _request(count: int) -> StoryCompileRequest:
    return StoryCompileRequest(
        selected_storyline="선택한 이야기", genre_tags=["무협"], protagonist={},
        supporting_characters=[{} for _ in range(count)],
    )


@pytest.mark.parametrize("provider", ["openai", "google"])
@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 5])
def test_count_rules_reach_compile_and_refill(provider: str, count: int) -> None:
    system, user, _ = build_compile_prompt(
        "이야기", "", ["무협"], CharacterInput(),
        [CharacterInput() for _ in range(count)], provider=provider,
    )
    assert f"입력 주변 인물 수: {count}명" in user
    assert "{{" not in user
    assert "입력 목록과 정확히 일대일" in system
    assert "주변 인물 1~5명을 자유롭게 구성" in system
    assert "남는 자리를 채워도" not in system
    refill_system, refill_user = build_refill_prompt(
        user, "{}", ["character_setting"], provider=provider,
    )
    assert refill_system == system
    assert refill_user.startswith(user)
    assert "각 input_character_id에 카드 하나씩만" in refill_user


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("input_present", [False, True])
async def test_matching_or_freely_generated_count_needs_no_refill(
    monkeypatch: pytest.MonkeyPatch, count: int, input_present: bool,
) -> None:
    data = _spec(count)
    if not input_present:
        for card in data["prompt_settings"]["character_setting"]:
            card["input_character_id"] = None
    # 카드 순서는 입력 순서와 달라도 ID가 일대일이면 유효하다.
    data["prompt_settings"]["character_setting"].reverse()
    complete = AsyncMock(return_value=(data, story_llm.LlmUsage("m", 1, 1, provider="openai")))
    monkeypatch.setattr(story_llm, "_complete_json", complete)

    result = await story_llm.compile_story(_request(count if input_present else 0))

    assert complete.await_count == 1
    assert len(result.character_appearances) == count
    assert result.meta.retry_count == 0


@pytest.mark.parametrize("ids", [
    ["input-1", "input-2", None],  # 초과
    ["input-1"],  # 누락
    ["input-1", "input-1"],  # 같은 수지만 중복
    ["input-1", "input-3"],  # 같은 수지만 다른 인물
    ["input-1", None],
    ["input-1", ["input-2"]],  # 형식 오류도 500 대신 보완
])
@pytest.mark.parametrize("recovers", [False, True])
async def test_invalid_roster_refills_before_images(
    monkeypatch: pytest.MonkeyPatch, ids: list, recovers: bool,
) -> None:
    invalid = _spec(len(ids))
    for card, value in zip(invalid["prompt_settings"]["character_setting"], ids):
        card["input_character_id"] = value
    fixed = _spec(2)
    calls: list[str] = []

    async def complete(system: str, user: str, **kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            data = deepcopy(invalid)
        else:
            data = {"character_setting": deepcopy(
                (fixed if recovers else invalid)["prompt_settings"]["character_setting"]
            )}
        return data, story_llm.LlmUsage("m", 10, 20, provider="openai")

    images = AsyncMock(return_value=[])
    thumbnail = AsyncMock(wraps=story_llm._generate_thumbnail_image_safe)
    monkeypatch.setattr(story_llm, "_complete_json", complete)
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", thumbnail)

    if recovers:
        result = await story_llm.compile_story(_request(2))
        assert len(calls) == 2
        assert len(result.character_appearances) == 2
        assert result.meta.retry_count == 1
        assert result.meta.input_token_count == 20
        assert result.meta.output_token_count == 40
        assert result.story_start_settings.prologue == invalid["start"]["prologue"]
        assert len(images.call_args.args[0]) == 2
        images.assert_awaited_once()
        thumbnail.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await story_llm.compile_story(_request(2))
        assert exc.value.status_code == 502
        assert len(calls) == 3
        images.assert_not_awaited()
        thumbnail.assert_not_awaited()
    assert "character_setting" in calls[1]
    assert "character_updates" not in calls[1]
