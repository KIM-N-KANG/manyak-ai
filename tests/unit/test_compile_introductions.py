"""KNK-1458: 공개 인물 소개의 응답 분리·필드 보완·실패 한도."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.schemas.story import CharacterInput
from src.schemas.story_compile import (
    CharacterIntroductionOut,
    StoryCompileRequest,
    StoryCompileResponse,
    StorySpec,
    ThumbnailImageOut,
)
from src.services import story_llm
from src.services.prompt import build_compile_prompt, build_refill_prompt
from src.services.story_compile_render import spec_to_response


def _spec() -> dict:
    return json.loads((Path(__file__).parent / "fixtures/spec_valid.json").read_text())


@pytest.mark.parametrize("description", [None, "", "   ", 123, [], "가" * 81, "첫째\n둘째", "첫째\r둘째", "첫째\t둘째"])
def test_invalid_description_uses_same_rules_for_schema_and_repairs(description: object) -> None:
    data = _spec()
    data["prompt_settings"]["character_setting"][1]["description"] = description
    assert story_llm._find_character_field_repairs(data) == {1: ("description",)}
    with pytest.raises(ValidationError):
        StorySpec.model_validate(data)
    with pytest.raises(ValidationError):
        CharacterIntroductionOut(name="세린", description=description)


def test_description_is_required_and_accepts_trimmed_80_characters() -> None:
    data = _spec()
    del data["prompt_settings"]["character_setting"][0]["description"]
    assert story_llm._find_character_field_repairs(data) == {0: ("description",)}
    with pytest.raises(ValidationError):
        StorySpec.model_validate(data)
    intro = CharacterIntroductionOut(name="레이", description="  " + "가" * 80 + "  ")
    assert intro.description == "가" * 80
    assert "character_introductions" in StoryCompileResponse.model_json_schema()["required"]


@pytest.mark.parametrize("provider", ["openai", "google"])
def test_compile_and_refill_receive_public_description_rules(provider: str) -> None:
    system, user, _ = build_compile_prompt(
        "이야기", "", ["판타지"], CharacterInput(), [], provider=provider,
    )
    assert "`description`: 작품 페이지" in system
    assert "공백 포함 80자 이내" in system
    assert "마크다운·줄바꿈·탭 없이" in system
    assert "숨겨진 동기·반전·결말은 밝히지 않는다" in system
    refill_system, refill_user = build_refill_prompt(user, "{}", [], {1: ("description",)}, provider=provider)
    assert refill_system == system
    assert "index 1: description" in refill_user


@pytest.mark.parametrize("count", [1, 5])
@pytest.mark.parametrize("named_input", [False, True])
async def test_introductions_match_final_names_and_preserve_chat_settings(
    monkeypatch: pytest.MonkeyPatch, count: int, named_input: bool,
) -> None:
    data = _spec()
    template = data["prompt_settings"]["character_setting"][0]
    cards = [dict(template, name=f"생성{i}", input_character_id=f"input-{i + 1}" if named_input else None,
                  description=f"사건의 단서를 모으는 동료 {i}.") for i in range(count)]
    data["prompt_settings"]["character_setting"] = cards
    cards.reverse()  # 입력 순서가 달라도 최종 이름과 소개가 같은 카드에 묶인다.
    complete = AsyncMock(return_value=(deepcopy(data), story_llm.LlmUsage("m", 1, 2, provider="openai")))
    monkeypatch.setattr(story_llm, "_complete_json", complete)
    request = StoryCompileRequest(
        selected_storyline="이야기", genre_tags=["판타지"], protagonist={"name": "주인공"},
        supporting_characters=[{"name": f"입력{i}"} for i in range(count)] if named_input else [],
    )
    result = await story_llm.compile_story(request)
    expected = [{"name": f"입력{i}" if named_input else f"생성{i}",
                 "description": f"사건의 단서를 모으는 동료 {i}."} for i in reversed(range(count))]
    assert [item.model_dump() for item in result.character_introductions] == expected
    assert [item.name for item in result.character_appearances] == [item["name"] for item in expected]
    for card, intro in zip(cards, expected):
        card["name"] = intro["name"]
    data["prompt_settings"]["user_role_setting"]["name"] = "주인공"
    baseline = spec_to_response(StorySpec.model_validate(data), thumbnail_image=ThumbnailImageOut(error="timeout"))
    assert result.story_settings == baseline.story_settings
    for intro in expected:
        assert intro["description"] not in result.story_settings.character_setting
    complete.assert_awaited_once()


@pytest.mark.parametrize("recovers", [False, True])
async def test_missing_introduction_refills_only_description_before_images(
    monkeypatch: pytest.MonkeyPatch, recovers: bool,
) -> None:
    data = _spec()
    del data["prompt_settings"]["character_setting"][1]["description"]
    refill = {"character_updates": [{"index": 1, "description": "냉철한 마법사. 주인공에게 거래를 제안한다." if recovers else "가" * 81,
                                      "name": "덮어쓰면 안 됨", "personality": "덮어쓰면 안 됨"}]}
    usage = story_llm.LlmUsage("m", 10, 20, provider="openai")
    complete = AsyncMock(side_effect=[(deepcopy(data), usage), (deepcopy(refill), usage), (deepcopy(refill), usage)])
    images = AsyncMock(return_value=[])
    thumbnail = AsyncMock(return_value=ThumbnailImageOut(error="timeout"))
    monkeypatch.setattr(story_llm, "_complete_json", complete)
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", thumbnail)
    request = StoryCompileRequest(selected_storyline="이야기", genre_tags=["판타지"], protagonist={}, supporting_characters=[{}, {}, {}])
    if recovers:
        result = await story_llm.compile_story(request)
        assert complete.await_count == 2
        assert result.character_introductions[1].model_dump() == {"name": "세린", "description": refill["character_updates"][0]["description"]}
        assert data["prompt_settings"]["character_setting"][1]["personality"] in result.story_settings.character_setting
        assert result.meta.retry_count == 1
        assert result.meta.input_token_count == 20
        assert result.meta.output_token_count == 40
        assert len(result.character_introductions) == 3  # 이미지 실패·빈 배열과 독립적이다.
        images.assert_awaited_once()
        thumbnail.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await story_llm.compile_story(request)
        assert exc.value.status_code == 502
        assert complete.await_count == 3
        images.assert_not_awaited()
        thumbnail.assert_not_awaited()
    assert "index 1: description" in complete.call_args_list[1].args[1]
