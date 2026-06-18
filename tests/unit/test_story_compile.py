import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.schemas.story_compile import StoryCompileRequest, StorySpec
from src.services import story_llm
from src.services.prompt import build_compile_prompt

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_valid_spec_parses() -> None:
    spec = StorySpec(**_load("spec_valid.json"))
    assert spec.meta.genre == "다크 판타지"
    assert 1 <= len(spec.prompt_settings.character_setting) <= 3
    assert spec.prompt_settings.plot_setting.premise
    assert spec.prompt_settings.user_role_setting.name
    assert len(spec.suggested_inputs) <= 3


def test_too_many_characters_rejected() -> None:
    with pytest.raises(ValidationError):
        StorySpec(**_load("spec_chars_4.json"))


def test_strip_code_fence() -> None:
    fenced = '```json\n{"a": 1}\n```'
    assert story_llm._strip_code_fence(fenced) == '{"a": 1}'
    # 펜스가 없으면 그대로
    assert story_llm._strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_build_compile_prompt_substitutes_all_slots() -> None:
    system, user = build_compile_prompt(
        "스토리라인 본문",
        "추가정보 본문",
        ["다크 판타지"],
        ["신중한"],
        ["충직한", "거친"],
    )
    assert system  # SYSTEM 블록 비어있지 않음
    assert "{{" not in user  # 자리표시자 잔류 없음
    assert "스토리라인 본문" in user
    assert "추가정보 본문" in user
    assert "다크 판타지" in user
    assert "충직한, 거친" in user


def test_build_compile_prompt_empty_extra_info() -> None:
    _, user = build_compile_prompt("라인", "", ["판타지"], ["용감한"], ["거친"])
    assert "{{" not in user
    assert "(없음)" in user


async def test_compile_story_injects_genre(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _load("spec_valid.json")
    data["meta"]["genre"] = "WRONG"  # LLM이 틀린 genre를 줘도

    async def fake_complete(system: str, user: str) -> dict:
        return data

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    request = StoryCompileRequest(
        selected_storyline="x",
        extra_info="",
        genre_tags=["다크 판타지", "느와르"],
        protagonist_tags=["신중한"],
        supporting_tags=["거친"],
    )
    spec = await story_llm.compile_story(request)
    # 입력 태그가 정본 — meta.genre를 덮어쓴다
    assert spec.meta.genre == "다크 판타지, 느와르"
