"""주인공 이름·성별 덮어쓰기와 통글 성별 칸(KNK-838) 검증.

계약(5-ai-server.md §5-3-3): 사용자가 입력한 주인공 이름은 LLM 출력에 맡기지 않고
코드가 주인공 통글에 덮어써 담보한다(장르 덮어쓰기와 같은 원칙, D7). 성별은 인물
카드 `### 성별`, 주인공 통글 `## 성별` 명시 칸으로 나간다. 칸 값의 품질은 실측 몫이다.
"""

import json
from pathlib import Path

import pytest

from src.schemas.story_compile import StoryCompileRequest, StorySpec
from src.services import story_llm
from src.services.story_compile_render import spec_to_response

_FIXTURES = Path(__file__).parent / "fixtures"


def _spec() -> dict:
    return json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8"))


def _request(protagonist: dict) -> StoryCompileRequest:
    return StoryCompileRequest(
        selected_storyline="x",
        genre_tags=["다크 판타지"],
        protagonist=protagonist,
    )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, data: dict) -> None:
    async def fake_complete(system: str, user: str, **kwargs: object):
        return json.loads(json.dumps(data)), story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)


# ── 통글 성별 칸 렌더링 ─────────────────────────────────────────────────────
def test_gender_sections_rendered() -> None:
    res = spec_to_response(StorySpec(**_spec()))
    # 인물 카드: 이름 바로 아래 ### 성별. fixture 첫 카드는 레이(남성).
    assert "## 레이\n### 성별\n남성\n### 성격" in res.story_settings.character_setting
    # 주인공 통글: 호칭 바로 아래 ## 성별.
    assert "## 성별\n남성\n## 역할" in res.story_settings.user_role_setting


# ── 주인공 이름·성별 덮어쓰기 ───────────────────────────────────────────────
async def test_protagonist_input_overrides_llm_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM이 뭐라고 썼든(기사님·남성) 입력 이름·성별이 최종값이다."""
    _patch_llm(monkeypatch, _spec())
    res = await story_llm.compile_story(_request({"name": "카일라", "gender": "FEMALE"}))
    ur = res.story_settings.user_role_setting
    assert "## 호칭\n카일라\n" in ur
    assert "## 성별\n여성\n" in ur
    assert "기사님" not in ur


async def test_protagonist_blank_keeps_llm_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """이름·성별을 비우면 LLM이 지은 값을 그대로 둔다."""
    _patch_llm(monkeypatch, _spec())
    res = await story_llm.compile_story(_request({"features": ["신중한"]}))
    ur = res.story_settings.user_role_setting
    assert "## 호칭\n기사님\n" in ur
    assert "## 성별\n남성\n" in ur


async def test_gender_only_input_keeps_llm_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """이름은 비우고 성별만 고른 입력 — 프론트에서 가장 흔한 모양이다.

    이름·성별은 각각 따로 판단해 덮어쓴다. 둘을 한 덩어리로 묶는 회귀(예: 이름이
    있을 때만 성별도 덮어쓰기)가 들어오면 이름을 안 낸 대다수 요청에서 성별
    덮어쓰기가 통째로 멈추므로, 반쪽 입력 양쪽을 다 고정한다.
    """
    _patch_llm(monkeypatch, _spec())
    res = await story_llm.compile_story(_request({"gender": "FEMALE"}))
    ur = res.story_settings.user_role_setting
    assert "## 호칭\n기사님\n" in ur  # 이름은 LLM 값 그대로
    assert "## 성별\n여성\n" in ur  # 성별만 입력값으로 교체


async def test_name_only_input_keeps_llm_gender(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, _spec())
    res = await story_llm.compile_story(_request({"name": "카일라"}))
    ur = res.story_settings.user_role_setting
    assert "## 호칭\n카일라\n" in ur
    assert "## 성별\n남성\n" in ur  # 성별은 LLM 값 그대로


async def test_input_reapplied_after_refill(monkeypatch: pytest.MonkeyPatch) -> None:
    """재호출이 주인공 블록을 통째로 갈아끼운 뒤에도 입력값이 최종값이다.

    주입은 본호출 직후뿐 아니라 재호출 병합 직후에도 실행돼야 한다. 그 재주입이
    빠지면 재호출이 데려온 LLM 이름·성별이 사용자가 고른 값을 되덮는다.
    """
    first = _spec()
    first["prompt_settings"]["user_role_setting"] = "엉터리"  # 블록 자체가 깨져 재호출 대상
    refilled = {"user_role_setting": _spec()["prompt_settings"]["user_role_setting"]}
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **kwargs: object):
        label = str(kwargs.get("label", "compile"))
        calls.append(label)
        data = json.loads(json.dumps(refilled if label.startswith("refill") else first))
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    res = await story_llm.compile_story(_request({"name": "카일라", "gender": "FEMALE"}))

    assert calls == ["compile", "refill#1"]  # 재호출 1회로 회복
    ur = res.story_settings.user_role_setting
    assert "## 호칭\n카일라\n" in ur
    assert "## 성별\n여성\n" in ur
    assert "기사님" not in ur  # 재호출이 데려온 LLM 값이 되살아나지 않는다


async def test_injection_fills_empty_llm_fields_without_refill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM이 이름·성별을 비워도 입력값 주입이 먼저라 refill 없이 채워진다."""
    data = _spec()
    data["prompt_settings"]["user_role_setting"]["name"] = ""
    data["prompt_settings"]["user_role_setting"]["gender"] = ""
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **kwargs: object):
        calls.append(str(kwargs.get("label", "compile")))
        return json.loads(json.dumps(data)), story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    res = await story_llm.compile_story(_request({"name": "카일라", "gender": "FEMALE"}))
    assert calls == ["compile"]
    assert "## 호칭\n카일라\n" in res.story_settings.user_role_setting


def test_inject_protagonist_survives_malformed_block() -> None:
    # LLM이 user_role_setting을 객체가 아닌 값으로 줘도 500으로 새지 않는다(refill 몫).
    data = {"prompt_settings": {"user_role_setting": "엉터리"}}
    story_llm._inject_protagonist(
        data, _request({"name": "카일라", "gender": "FEMALE"}).protagonist
    )
    assert data["prompt_settings"]["user_role_setting"] == "엉터리"


# ── 성별 빈 값은 재호출 대상 ────────────────────────────────────────────────
def test_missing_gender_detected_as_refill_target() -> None:
    data = _spec()
    del data["prompt_settings"]["character_setting"][0]["gender"]
    data["prompt_settings"]["user_role_setting"]["gender"] = ""
    missing = story_llm._find_missing_keys(data)
    assert "prompt_settings.character_setting[0].gender" in missing
    assert "prompt_settings.user_role_setting.gender" in missing
