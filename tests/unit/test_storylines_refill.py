"""인물이 빠진 스토리라인 편만 부분 재호출(KNK-840) 배선 검증.

실측에서 세 편 중 한 편만 인물이 빠지는 경우가 나왔다. 그때 세 편을 통째로 다시 사면
잘 나온 편까지 버리고 출력·대기 시간이 3배가 되므로, 빠진 편만 다시 받아 그 자리에
끼워 넣는다. 다시 쓴 편의 품질·다양성은 실측 몫이고, 여기서는 배선만 고정한다.
"""

import json

import pytest

from src.schemas.story import CharacterInput
from src.services import story_llm
from src.services.prompt import build_storylines_prompt, build_storylines_refill_prompt


def _stories(*storylines: str) -> dict:
    return {
        "stories": [
            {"id": i + 1, "storyline": s, "recommended_infos": ["가", "나", "다"]}
            for i, s in enumerate(storylines)
        ]
    }


def _prompts() -> tuple[str, str]:
    return build_storylines_prompt(
        ["무협"], CharacterInput(), [CharacterInput(name="서린", gender="FEMALE")]
    )


async def test_refills_only_missing_story(monkeypatch: pytest.MonkeyPatch) -> None:
    """2편만 인물이 빠지면 그 편만 다시 받고, 나머지 두 편은 첫 응답 그대로 남는다."""
    calls: list[tuple[str, str]] = []

    async def fake_complete(system: str, user: str, *args: object, **kwargs: object):
        label = str(kwargs.get("label", "storylines"))
        calls.append((label, user))
        if label.startswith("storylines-refill"):
            data = {
                "stories": [
                    {"id": 2, "storyline": "서린이 돌아온 2편", "recommended_infos": ["가", "나", "다"]}
                ]
            }
            return data, story_llm.LlmUsage("m", 7, 9, provider="deepseek")
        data = _stories("서린 1편", "낯선 사내만 나온 2편", "서린 3편")
        return data, story_llm.LlmUsage("m", 100, 200, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    system, user = _prompts()
    result, usage = await story_llm.generate_storylines(system, user, required_names=["서린"])

    assert [label for label, _ in calls] == ["storylines", "storylines-refill#1"]
    assert [s["storyline"] for s in result["stories"]] == [
        "서린 1편",
        "서린이 돌아온 2편",
        "서린 3편",
    ]
    assert [s["id"] for s in result["stories"]] == [1, 2, 3]  # id는 코드가 다시 박는다
    # 재호출 프롬프트는 어느 편을 다시 쓸지 지목하고, 나머지 편을 맥락으로 함께 준다.
    refill_user = calls[1][1]
    assert "2편" in refill_user
    assert "서린 3편" in refill_user
    # 토큰은 본호출+재호출 합산, retry_count는 부분 재호출 횟수를 포함한다.
    assert (usage.input_tokens, usage.output_tokens) == (107, 209)
    assert usage.retry_count == 1


async def test_no_refill_when_all_stories_have_names(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_complete(system: str, user: str, *args: object, **kwargs: object):
        calls.append(str(kwargs.get("label", "storylines")))
        return _stories("서린 1편", "서린 2편", "서린 3편"), story_llm.LlmUsage(
            "m", 1, 1, provider="deepseek"
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    system, user = _prompts()
    _, usage = await story_llm.generate_storylines(system, user, required_names=["서린"])
    assert calls == ["storylines"]  # 한 번에 통과
    assert usage.retry_count == 0


async def test_returns_result_with_warning_after_two_refills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """두 번 다시 받아도 인물이 빠지면 502가 아니라 결과를 그대로 내주고 Sentry 경고만 남긴다.

    KNK-1102 회귀 그물: 특정 입력은 재호출로도 이름이 안 들어가 사용자 입장에서 영구
    실패였다(2026-09-01 운영 502 7건, 전부 한 세션). 이름 미등장은 품질 흠이지 못 쓸
    결과가 아니므로 내주는 쪽으로 완화했다.
    """
    calls: list[str] = []
    captured: list[dict] = []

    async def fake_complete(system: str, user: str, *args: object, **kwargs: object):
        label = str(kwargs.get("label", "storylines"))
        calls.append(label)
        if label.startswith("storylines-refill"):
            # 회차별로 본문을 다르게 줘서 마지막 재호출 결과가 실제로 병합됐는지 고정한다
            # (같은 본문이면 1회차만 병합돼도 통과해 버린다 — Codex 리뷰).
            data = {
                "stories": [
                    {"id": 2, "storyline": f"여전히 없는 2편({label})", "recommended_infos": ["가", "나", "다"]}
                ]
            }
        else:
            data = _stories("서린 1편", "없는 2편", "서린 3편")
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(
        story_llm, "capture_ai_exception", lambda exc, **kw: captured.append({"exc": exc, **kw})
    )
    system, user = _prompts()
    result, usage = await story_llm.generate_storylines(system, user, required_names=["서린"])

    assert calls == ["storylines", "storylines-refill#1", "storylines-refill#2"]
    # 마지막 재호출 결과까지 병합된 세 편이 그대로 나간다.
    assert [s["storyline"] for s in result["stories"]] == [
        "서린 1편",
        "여전히 없는 2편(storylines-refill#2)",
        "서린 3편",
    ]
    assert usage.retry_count == 2
    # Sentry에는 경고 수준으로 1건만 보고한다.
    assert len(captured) == 1
    assert captured[0]["level"] == "warning"
    assert captured[0]["retry_count"] == 2


async def test_refill_breaking_contract_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """재호출이 못 쓸 편을 데려오면(추천 정보 부족) 원본으로 되돌리고, 소진 후 원본을 내준다.

    깨진 편이 병합돼 조립에서 500으로 터지면 안 되고(계약 보호), 이름만 빠진 유효한
    원본은 502로 버리지 않는다(KNK-1102).
    """
    captured: list[dict] = []

    async def fake_complete(system: str, user: str, *args: object, **kwargs: object):
        label = str(kwargs.get("label", "storylines"))
        if label.startswith("storylines-refill"):
            data = {"stories": [{"id": 2, "storyline": "서린 2편", "recommended_infos": ["가"]}]}
        else:
            data = _stories("서린 1편", "없는 2편", "서린 3편")
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(
        story_llm, "capture_ai_exception", lambda exc, **kw: captured.append({"exc": exc, **kw})
    )
    system, user = _prompts()
    result, _usage = await story_llm.generate_storylines(system, user, required_names=["서린"])
    # 깨진 재호출 편은 버려지고 계약이 유효한 원본이 그대로 나간다.
    assert [s["storyline"] for s in result["stories"]] == ["서린 1편", "없는 2편", "서린 3편"]
    assert all(len(s["recommended_infos"]) == 3 for s in result["stories"])
    assert len(captured) == 1 and captured[0]["level"] == "warning"


def test_refill_prompt_asks_to_fix_not_rewrite() -> None:
    """재호출 지시가 '다시 써라'가 아니라 '고쳐라'인지, 이름만 얹지 말라는 경고가 있는지 확인."""
    _, user = _prompts()
    _, refill_user = build_storylines_refill_prompt(user, json.dumps(_stories("가", "나", "다")), [2, 3])
    assert "2번 이야기, 3번 이야기" in refill_user
    assert "흐름은 그대로 유지" in refill_user
    assert "이름만 한 줄 얹지 말고" in refill_user
    assert "나머지 이야기는 그대로 두므로 응답에 포함하지 마라" in refill_user
    assert "stories" in refill_user
