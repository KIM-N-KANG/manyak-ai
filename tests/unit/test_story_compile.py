import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.schemas.story import CharacterInput
from src.schemas.story_compile import (
    LorebookItem,
    StoryCompileRequest,
    StoryCompileResponse,
    StorySpec,
)
from src.services import story_llm
from src.services.prompt import build_compile_prompt, build_refill_prompt
from src.services.story_compile_render import spec_to_response

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _request() -> StoryCompileRequest:
    return StoryCompileRequest(
        selected_storyline="x",
        additional_info="",
        genre_tags=["다크 판타지", "느와르"],
        protagonist={"features": ["신중한"]},
        supporting_characters=[{"features": ["거친"]}],
    )


# ── 스키마 ──────────────────────────────────────────────────────────────────
def test_valid_spec_parses() -> None:
    spec = StorySpec(**_load("spec_valid.json"))
    assert spec.meta.genre == "다크 판타지"
    assert 1 <= len(spec.prompt_settings.character_setting) <= 5
    assert spec.prompt_settings.plot_setting.premise
    assert spec.prompt_settings.user_role_setting.name
    assert len(spec.suggested_inputs) == 3


def test_too_many_characters_rejected() -> None:
    # 인물 6명 — 상한 5 초과로 거부(suggested_inputs는 정상 3개라 거부 사유는 인물 수뿐)
    with pytest.raises(ValidationError):
        StorySpec(**_load("spec_chars_6.json"))


def test_five_characters_allowed() -> None:
    # 상한이 5명이므로 5명은 통과해야 한다.
    data = _load("spec_valid.json")
    base = data["prompt_settings"]["character_setting"][0]
    data["prompt_settings"]["character_setting"] = [
        dict(base, name=f"인물{i}") for i in range(5)
    ]
    spec = StorySpec(**data)
    assert len(spec.prompt_settings.character_setting) == 5


# ── 로어북 (KNK-422) ─────────────────────────────────────────────────────────
def test_lorebooks_default_empty_backward_compatible() -> None:
    # 로어북을 안 보내는 기존 요청은 그대로 통과하고 기본값은 빈 배열(하위호환).
    assert _request().lorebooks == []


def test_lorebooks_parse_when_provided() -> None:
    req = StoryCompileRequest(
        selected_storyline="x",
        genre_tags=["무협"],
        protagonist={"features": ["신중한"]},
        supporting_characters=[{"features": ["거친"]}],
        lorebooks=[{"name": "내공", "content": "기를 단전에 쌓아 다스리는 힘."}],
    )
    assert len(req.lorebooks) == 1
    assert req.lorebooks[0].name == "내공"
    assert req.lorebooks[0].content


def test_lorebooks_explicit_null_accepted() -> None:
    # 명시적 null도 "없음"으로 받아 요청 전체가 실패하지 않는다(Gemini 리뷰 — 미전달과 동치).
    req = StoryCompileRequest(
        selected_storyline="x",
        genre_tags=["무협"],
        protagonist={"features": ["신중한"]},
        supporting_characters=[{"features": ["거친"]}],
        lorebooks=None,
    )
    assert req.lorebooks is None


# ── 프롬프트 ────────────────────────────────────────────────────────────────
def test_strip_code_fence() -> None:
    fenced = '```json\n{"a": 1}\n```'
    assert story_llm._strip_code_fence(fenced) == '{"a": 1}'
    # 펜스가 없으면 그대로
    assert story_llm._strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_build_compile_prompt_substitutes_all_slots() -> None:
    system, user, _ = build_compile_prompt(
        "스토리라인 본문",
        "추가정보 본문",
        ["다크 판타지"],
        CharacterInput(features=["신중한"]),
        [CharacterInput(features=["충직한", "거친"])],
    )
    assert system  # SYSTEM 블록 비어있지 않음
    assert "{{" not in user  # 자리표시자 잔류 없음
    assert "스토리라인 본문" in user
    assert "추가정보 본문" in user
    assert "다크 판타지" in user
    assert "충직한, 거친" in user


def test_build_compile_prompt_empty_additional_info() -> None:
    _, user, _ = build_compile_prompt(
        "라인", "", ["판타지"], CharacterInput(features=["용감한"]), [CharacterInput(features=["거친"])]
    )
    assert "{{" not in user
    assert "(없음)" in user


def test_build_compile_prompt_injects_lorebooks() -> None:
    # 로어북이 있으면 {{로어북}} 슬롯에 이름·내용이 실려 프롬프트에 들어간다.
    _, user, _ = build_compile_prompt(
        "라인", "정보", ["무협"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])],
        [LorebookItem(name="내공", content="기를 단전에 쌓아 다스리는 힘.")],
    )
    assert "{{" not in user
    assert "내공" in user
    assert "기를 단전에 쌓아 다스리는 힘." in user


def test_build_compile_prompt_multiple_lorebooks() -> None:
    # 로어북이 여러 개면 각 항목이 ### 헤더 블록으로 실린다(bullet 인라인 아님).
    _, user, _ = build_compile_prompt(
        "라인", "정보", ["무협"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])],
        [
            LorebookItem(name="내공", content="기를 단전에 쌓아 다스리는 힘."),
            LorebookItem(name="외공", content="몸을 단련해 얻는 힘."),
        ],
    )
    assert "### 내공" in user
    assert "### 외공" in user
    assert "기를 단전에 쌓아 다스리는 힘." in user
    assert "몸을 단련해 얻는 힘." in user


def test_build_compile_prompt_strips_lorebook_whitespace() -> None:
    # 이름·내용 앞뒤 공백·개행이 있어도 ### 헤더·문단이 깔끔히 렌더된다(Gemini 리뷰).
    _, user, _ = build_compile_prompt(
        "라인", "정보", ["무협"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])],
        [LorebookItem(name="  내공\n", content="\n기를 단전에 쌓는 힘.  ")],
    )
    # 앞뒤 공백·개행이 제거돼 헤더·내용이 한 줄씩 붙는다(여분 공백·빈 줄 없음).
    assert "### 내공\n기를 단전에 쌓는 힘." in user


def test_build_compile_prompt_null_lorebooks_slot() -> None:
    # lorebooks=None(명시적 null)도 (없음)으로 채워져 빈 배열·미전달과 동일하다.
    _, user, _ = build_compile_prompt(
        "라인", "정보", ["무협"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])], None
    )
    assert "{{" not in user
    assert "참고 로어북(장르 용어 사전):" in user
    assert "(없음)" in user


def test_build_compile_prompt_empty_lorebooks_slot() -> None:
    # 로어북 미전달·빈 배열이면 {{로어북}} 슬롯이 (없음)으로 채워지고(미주입), 두 경로가 동일하다.
    _, user_none, _ = build_compile_prompt(
        "라인", "정보", ["무협"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])]
    )
    _, user_empty, _ = build_compile_prompt(
        "라인", "정보", ["무협"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])], []
    )
    assert "{{" not in user_none
    assert "참고 로어북(장르 용어 사전):" in user_none  # 하단 별도 블록 라벨
    assert "(없음)" in user_none  # 슬롯이 비주입 마커로 치환됨
    assert user_none == user_empty


# ── genre 주입 ──────────────────────────────────────────────────────────────
def test_inject_genre_overwrites() -> None:
    data = {"meta": {"genre": "WRONG"}}
    story_llm._inject_genre(data, ["다크 판타지", "느와르"])
    assert data["meta"]["genre"] == "다크 판타지, 느와르"


# ── 빈 필수키 검증 ──────────────────────────────────────────────────────────
def test_find_missing_keys_clean() -> None:
    assert story_llm._find_missing_keys(_load("spec_valid.json")) == []


def test_find_missing_keys_allows_empty_preference_and_genre() -> None:
    data = _load("spec_valid.json")
    data["prompt_settings"]["user_role_setting"]["preference"] = ""
    data["meta"]["genre"] = ""
    # preference(선택)·genre(코드가 덮어씀)는 빈 값이어도 통과
    assert story_llm._find_missing_keys(data) == []


def test_find_missing_keys_detects_empty_required() -> None:
    data = _load("spec_valid.json")
    data["prompt_settings"]["world_setting"] = "   "  # 공백만
    data["start"]["prologue"] = ""
    data["prompt_settings"]["character_setting"][1]["tone"] = ""
    missing = story_llm._find_missing_keys(data)
    assert "prompt_settings.world_setting" in missing
    assert "start.prologue" in missing
    assert "prompt_settings.character_setting[1].tone" in missing


def test_find_missing_keys_requires_exactly_three_inputs() -> None:
    data = _load("spec_valid.json")
    data["suggested_inputs"] = ["하나", "둘"]  # 2개
    assert "suggested_inputs" in story_llm._find_missing_keys(data)


def test_find_missing_keys_tolerates_wrong_types() -> None:
    # LLM이 객체 자리에 문자열·문자열 배열을 줘도 500이 아니라 missing으로 수집해야 한다.
    data = _load("spec_valid.json")
    data["meta"] = "문자열로 잘못 옴"
    data["prompt_settings"]["character_setting"] = ["레이", "세린"]
    missing = story_llm._find_missing_keys(data)
    assert "meta.title" in missing
    assert "prompt_settings.character_setting[0].gender" in missing


def test_block_of_maps_paths() -> None:
    assert story_llm._block_of("meta.title") == "meta"
    assert story_llm._block_of("prompt_settings.world_setting") == "world_setting"
    assert story_llm._block_of("prompt_settings.plot_setting.premise") == "plot_setting"
    assert story_llm._block_of("prompt_settings.character_setting[1].tone") == "character_setting"
    assert story_llm._block_of("start.prologue") == "start"
    assert story_llm._block_of("suggested_inputs[0]") == "suggested_inputs"


# ── 통글 변환 ───────────────────────────────────────────────────────────────
def test_spec_to_response_renders_nested_markdown() -> None:
    spec = StorySpec(**_load("spec_valid.json"))
    res = spec_to_response(spec)

    assert isinstance(res, StoryCompileResponse)
    # stories: 값 그대로 + genre 제외
    assert res.stories.title == "잿빛 왕관"
    assert not hasattr(res.stories, "genre")
    # story_settings: 통글 마크다운 + 레이어 분배(plot/tone/length 흡수)
    assert res.story_settings.world_setting.startswith("# 세계관")
    assert "# 전제" in res.story_settings.world_setting
    assert "# 갈등" in res.story_settings.world_setting
    assert "## 레이" in res.story_settings.character_setting
    assert "### 말투" in res.story_settings.character_setting
    assert "# 문체 톤" in res.story_settings.rule_setting
    assert "# 분량 배분" in res.story_settings.rule_setting
    # 시작 설정·추천 입력은 값 그대로
    assert res.story_start_settings.name == "선왕의 장례식 날"
    assert len(res.story_suggested_inputs) == 3
    # character_appearances: 인물 전원의 외형 정보가 별도 배열로 내려온다
    assert len(res.character_appearances) == len(spec.prompt_settings.character_setting)
    for app, src in zip(res.character_appearances, spec.prompt_settings.character_setting, strict=True):
        assert app.name == src.name
        assert app.gender == src.gender
        assert app.age == src.age
        assert app.face == src.face
        assert app.hair == src.hair
        assert app.outfit == src.outfit
        assert app.visual_identity == src.visual_identity


def test_spec_to_response_render_equality() -> None:
    # 동타입 필드가 뒤바뀌어도(prologue↔start_situation·description↔key_sentence) truthy
    # 단언은 통과하므로, 픽스처 실값과 '정확히 일치'로 못박아 필드 뒤바뀜·렌더 형식 회귀를 잡는다.
    data = _load("spec_valid.json")
    res = spec_to_response(StorySpec(**data))
    ps = data["prompt_settings"]

    # pass-through 필드 — 원본과 정확히 일치
    assert res.story_start_settings.prologue == data["start"]["prologue"]
    assert res.story_start_settings.start_situation == data["start"]["start_situation"]
    assert res.stories.one_line_intro == data["meta"]["one_line_intro"]
    assert res.stories.description == data["meta"]["description"]

    # 사건·엔딩 — 항목별 필드 정확 일치(description↔key_sentence 뒤바뀜 방지)
    for got, src in zip(res.story_main_events, data["main_events"], strict=True):
        assert (got.name, got.description, got.key_sentence) == (
            src["name"], src["description"], src["key_sentence"]
        )
    for got, src in zip(res.story_endings, data["endings"], strict=True):
        assert (got.name, got.min_turns, got.achievement_condition, got.epilogue) == (
            src["name"], src["min_turns"], src["achievement_condition"], src["epilogue"]
        )

    # 렌더 통글 — f-string 산출물 전체 문자열 등가로 고정
    assert res.story_settings.world_setting == (
        f"# 세계관\n{ps['world_setting']}\n\n"
        f"# 전제\n{ps['plot_setting']['premise']}\n\n"
        f"# 갈등\n{ps['plot_setting']['conflict']}"
    )
    assert res.story_settings.rule_setting == (
        f"# 전개 규칙\n{ps['rule_setting']}\n\n"
        f"# 문체 톤\n{ps['tone_setting']}\n\n"
        f"# 분량 배분\n{ps['length_ratio']}"
    )


# ── compile_story 통합 ──────────────────────────────────────────────────────
async def test_compile_story_returns_nested_response(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_complete(system: str, user: str, **_kwargs: object):
        # 완전한 결과 → 재호출 없음. (dict, 사용 메타) 튜플 반환.
        return _load("spec_valid.json"), story_llm.LlmUsage(
            "deepseek-test", 100, 200, provider="not-deepseek"
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert isinstance(res, StoryCompileResponse)
    assert res.stories.title == "잿빛 왕관"
    assert "## 레이" in res.story_settings.character_setting
    assert len(res.story_suggested_inputs) == 3
    # 로깅 메타(KNK-243): model=응답값, prompt_versions=객체, retry_count=0(재호출 없음)
    assert res.meta is not None
    assert res.meta.model == "deepseek-test"
    # 주입한 값이 그대로 meta까지 온다 — 상수로 되돌리면 여기서 깨진다(KNK-674 리뷰 H1).
    assert res.meta.provider == "not-deepseek"
    assert list(res.meta.prompt_versions) == ["COMPILE", "CHARACTER_IMAGE"]
    assert res.meta.prompt_versions["COMPILE"] >= 1
    assert res.meta.prompt_versions["CHARACTER_IMAGE"] >= 1
    assert res.meta.input_token_count == 100
    assert res.meta.output_token_count == 200
    assert res.meta.retry_count == 0


async def test_compile_story_forwards_lorebooks_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # compile_story가 request.lorebooks를 실제 프롬프트에 실어 보내는지 배선을 고정한다.
    # (build_compile_prompt 인자에서 request.lorebooks를 지우면 이 테스트가 깨진다 — 회귀 방지.)
    captured: dict[str, str] = {}

    async def fake_complete(system: str, user: str, **_kwargs: object):
        captured["user"] = user
        return _load("spec_valid.json"), story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    req = StoryCompileRequest(
        selected_storyline="x",
        genre_tags=["무협"],
        protagonist={"features": ["신중한"]},
        supporting_characters=[{"features": ["거친"]}],
        lorebooks=[LorebookItem(name="내공", content="기를 단전에 쌓아 다스리는 힘.")],
    )
    await story_llm.compile_story(req)
    assert "내공" in captured["user"]
    assert "기를 단전에 쌓아 다스리는 힘." in captured["user"]


async def test_compile_story_refills_missing_block(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            data = _load("spec_valid.json")
            data["prompt_settings"]["world_setting"] = ""  # 1차: 빈 필드
            return data, story_llm.LlmUsage("m", 100, 200, provider="deepseek")
        return {"world_setting": "복구된 세계관 설정"}, story_llm.LlmUsage("m", 10, 20, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert len(calls) == 2  # 최초 1 + 부분 재호출 1
    assert "복구된 세계관 설정" in res.story_settings.world_setting
    # 재호출 프롬프트에 '무엇을 보냈는가'도 계약이다 — 누락 블록명·직전 JSON과
    # 요청한 값만 보내라는 지시가 있어야 정밀 회복이 전체 재시도로 퇴화하지 않는다.
    refill_prompt = calls[1]
    assert "world_setting" in refill_prompt  # 누락 블록명
    assert "직전 생성 결과" in refill_prompt  # 직전 JSON 맥락
    assert "요청한 블록" in refill_prompt
    assert "요청하지 않은 블록과 인물 필드는 절대 포함하지 말 것" in refill_prompt
    # retry_count=재호출 횟수, 토큰은 본호출+재호출 합산
    assert res.meta.retry_count == 1
    assert res.meta.input_token_count == 110  # 100 + 10
    assert res.meta.output_token_count == 220  # 200 + 20


async def test_compile_story_502_after_max_refill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append(user)
        data = _load("spec_valid.json")
        data["start"]["prologue"] = ""  # 매번 빈 채 → 재호출로도 못 채움
        return data, story_llm.LlmUsage("m", 1, 1, provider="not-deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    captured: list = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda *a, **k: captured.append(k))

    with pytest.raises(HTTPException) as exc:
        await story_llm.compile_story(_request())
    assert exc.value.status_code == 502
    # 회복의 한계도 계약이다 — 총 3회 호출(최초 1 + 재호출 상한 2)에서 멈추고,
    # 소진 캡처의 retry_count가 상한값이다(루프 조건이 <=로 바뀌어 3회 재호출되면 깨진다).
    assert len(calls) == 3
    assert captured[-1]["retry_count"] == 2
    # Sentry provider 태그도 호출이 실제로 나간 공급자를 가리킨다 — 상수로 되돌리면
    # 다른 회사에서 난 실패가 전부 deepseek 탓으로 쌓인다(KNK-674 리뷰 H2).
    assert captured[-1]["provider"] == "not-deepseek"


# ── 재호출 프롬프트 내용·토큰 합산·경계값 (KNK-574 감사 1-4) ──────────────────
def test_build_compile_prompt_gemini_uses_gemini_template() -> None:
    """provider="google"이면 Gemini용 템플릿과 version_key를 반환한다(KNK-958)."""
    system_terra, user_terra, vk_terra = build_compile_prompt(
        "라인", "정보", ["판타지"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])],
    )
    system_gemini, user_gemini, vk_gemini = build_compile_prompt(
        "라인", "정보", ["판타지"], CharacterInput(features=["신중한"]), [CharacterInput(features=["거친"])],
        provider="google",
    )
    # version_key가 다르다
    assert vk_terra == "COMPILE"
    assert vk_gemini == "COMPILE_GEMINI"
    # system prompt가 다르다 (같은 모델이 아니라 다른 템플릿)
    assert system_terra != system_gemini
    # user prompt는 슬롯이 같으므로 내용이 같다
    assert "라인" in user_gemini
    assert "{{" not in user_gemini


def test_build_refill_prompt_gemini_uses_gemini_system() -> None:
    """provider="google"이면 refill도 Gemini system prompt를 쓴다(KNK-958)."""
    system_terra, _ = build_refill_prompt("원본", '{}', ["world_setting"])
    system_gemini, _ = build_refill_prompt("원본", '{}', ["world_setting"], provider="google")
    assert system_terra != system_gemini  # 서로 다른 system prompt


def test_build_refill_prompt_contains_context() -> None:
    # 재호출 프롬프트는 원본 맥락 + 누락 블록명 + 직전 JSON + 요청한 값만 반환하라는 지시를 담는다.
    system, user = build_refill_prompt(
        "원본 유저 프롬프트", '{"world_setting": ""}', ["world_setting", "start"]
    )
    assert system  # COMPILE 시스템 프롬프트 재사용(비어 있지 않음)
    assert "원본 유저 프롬프트" in user
    assert "world_setting, start" in user  # 누락 블록명 나열
    assert '{"world_setting": ""}' in user  # 직전 생성 결과(JSON)
    assert "한 번의 응답에서 모두 고쳐라" in user
    assert "요청한 블록" in user
    assert "요청하지 않은 블록과 인물 필드는 절대 포함하지 말 것" in user


def test_build_refill_prompt_contains_character_field_targets() -> None:
    system, user = build_refill_prompt(
        "원본",
        '{"prompt_settings": {"character_setting": []}}',
        ["start"],
        {0: ("hair",), 2: ("name", "outfit")},
    )
    assert "부분 재호출" in system
    assert '"character_updates"' in system
    assert '"index": 0' in system
    assert "start" in user
    assert "index 0: hair" in user
    assert "index 2: name, outfit" in user
    assert "character_updates" in user
    assert "배열 index는 0부터 시작" in user


def test_add_tokens_mixed_none() -> None:
    # 재호출 토큰 합산 — 둘 다 None이면 None, (None,int) 혼합은 누락을 0으로 본다.
    assert story_llm._add_tokens(None, None) is None
    assert story_llm._add_tokens(5, None) == 5
    assert story_llm._add_tokens(None, 7) == 7
    assert story_llm._add_tokens(3, 4) == 7


async def test_compile_story_refill_boundary_retry_two(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1·2차는 빈 채, 3차(2번째 재호출)에 채워 성공 — retry_count가 상한 2에 정확히 닿고,
    # 토큰은 3회 합산되며 (None,int) 혼합도 올바로 더해진다.
    calls: list[str] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append(user)
        if len(calls) == 1:
            data = _load("spec_valid.json")
            data["prompt_settings"]["world_setting"] = ""
            return data, story_llm.LlmUsage("m", 100, 200, provider="deepseek")
        if len(calls) == 2:
            return {"world_setting": ""}, story_llm.LlmUsage("m", None, 20, provider="deepseek")  # 혼합: input None
        return {"world_setting": "채움"}, story_llm.LlmUsage("m", 10, 30, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert len(calls) == 3  # 최초 1 + 재호출 2(상한)
    assert res.meta.retry_count == 2
    assert res.meta.input_token_count == 110  # 100 + 0(None) + 10
    assert res.meta.output_token_count == 250  # 200 + 20 + 30


# ── Sentry 캡처 경계(KNK-262) — 성공은 조용, 실패만 보고 ──────────────────────
async def test_compile_story_success_does_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 컴파일에서는 Sentry capture를 호출하지 않는다."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _load("spec_valid.json"), story_llm.LlmUsage("deepseek-test", 100, 200, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    calls: list = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda *a, **k: calls.append(1))

    res = await story_llm.compile_story(_request())
    assert res.stories.title  # 성공
    assert calls == []  # 성공 경로 — 미호출


async def test_compile_story_schema_failure_captures(monkeypatch: pytest.MonkeyPatch) -> None:
    """StorySpec 파싱 실패(인물 6명 > 상한 5)는 schema_validation_failed로 캡처한다."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        # 빈 필수키는 없어 재호출 없이 통과하지만, 인물 6명이라 StorySpec 파싱에서 거부된다.
        return _load("spec_chars_6.json"), story_llm.LlmUsage("m", 1, 1, provider="not-deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    calls: list = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda *a, **k: calls.append(k))

    with pytest.raises(HTTPException) as exc:
        await story_llm.compile_story(_request())
    assert exc.value.status_code == 502
    assert len(calls) == 1
    assert calls[0]["error_code"] == "schema_validation_failed"
    assert calls[0]["provider"] == "not-deepseek"  # provider 태그도 함께(KNK-674 리뷰 H2)


# ── 엔딩·주요 사건 (KNK-417) ─────────────────────────────────────────────────
def test_spec_to_response_passes_events_and_endings() -> None:
    # 엔딩·사건이 응답에 항목별 이산 필드로 실려야 한다(회귀 방지).
    spec = StorySpec(**_load("spec_valid.json"))
    res = spec_to_response(spec)
    assert 3 <= len(res.story_main_events) <= 5
    ev = res.story_main_events[0]
    assert ev.name and ev.description and ev.key_sentence
    assert len(res.story_endings) == 3
    end = res.story_endings[0]
    assert end.name and end.achievement_condition and end.epilogue
    assert isinstance(end.min_turns, int)


def test_endings_wrong_count_rejected() -> None:
    # 엔딩은 0개(폴백) 또는 정확히 3개. 2개·4개는 거부.
    for n in (2, 4):
        data = _load("spec_valid.json")
        base = data["endings"][0]
        data["endings"] = [dict(base, name=f"엔딩{i}") for i in range(n)]
        with pytest.raises(ValidationError):
            StorySpec(**data)


def test_zero_endings_allowed() -> None:
    # 빈 배열(폴백)은 계약상 허용된다 — StorySpec 파싱·응답 변환 모두 통과.
    data = _load("spec_valid.json")
    data["endings"] = []
    spec = StorySpec(**data)
    assert spec.endings == []
    assert spec_to_response(spec).story_endings == []


def test_min_turns_lower_bound_rejected() -> None:
    # min_turns는 1 이상(ge=1). 0·음수는 "최소 턴 문턱"이 무의미해져 거부.
    for bad in (0, -1):
        data = _load("spec_valid.json")
        data["endings"][0]["min_turns"] = bad
        with pytest.raises(ValidationError):
            StorySpec(**data)


def test_events_out_of_range_rejected() -> None:
    # 사건은 3~5개. 2개(하한 위반)·6개(상한 위반) 모두 거부.
    for n in (2, 6):
        data = _load("spec_valid.json")
        base = data["main_events"][0]
        data["main_events"] = [dict(base, name=f"사건{i}") for i in range(n)]
        with pytest.raises(ValidationError):
            StorySpec(**data)


def test_find_missing_keys_detects_event_ending_issues() -> None:
    # 사건 개수 하한 위반
    data = _load("spec_valid.json")
    data["main_events"] = data["main_events"][:2]
    assert "main_events" in story_llm._find_missing_keys(data)
    # 사건 항목의 빈 필드
    data = _load("spec_valid.json")
    data["main_events"][0]["key_sentence"] = ""
    assert "main_events[0].key_sentence" in story_llm._find_missing_keys(data)
    # 엔딩 항목의 빈 필드
    data = _load("spec_valid.json")
    data["endings"][1]["epilogue"] = "   "
    assert "endings[1].epilogue" in story_llm._find_missing_keys(data)
    # 엔딩 min_turns 비정수는 재호출 대상으로 잡힌다
    data = _load("spec_valid.json")
    data["endings"][0]["min_turns"] = "열다섯"
    assert "endings[0].min_turns" in story_llm._find_missing_keys(data)
    # 엔딩 min_turns 하한 미달(0·음수)도 재호출 대상으로 잡힌다(ge=1 강제와 정합)
    data = _load("spec_valid.json")
    data["endings"][0]["min_turns"] = 0
    assert "endings[0].min_turns" in story_llm._find_missing_keys(data)
    data = _load("spec_valid.json")
    data["endings"][1]["min_turns"] = -3
    assert "endings[1].min_turns" in story_llm._find_missing_keys(data)
    # int()가 못 바꾸는 유니코드 숫자('²')는 예외 없이 재호출 대상으로 잡힌다(500 방지 — Gemini 리뷰)
    data = _load("spec_valid.json")
    data["endings"][0]["min_turns"] = "²"  # 위첨자 2: isdigit()=True지만 int()는 ValueError
    assert "endings[0].min_turns" in story_llm._find_missing_keys(data)
    # 엔딩 개수 위반(2개)도 endings 블록으로 잡힌다
    data = _load("spec_valid.json")
    data["endings"] = data["endings"][:2]
    assert "endings" in story_llm._find_missing_keys(data)


def test_find_missing_keys_clean_with_events_and_endings() -> None:
    # 정상 spec은 엔딩·사건 때문에 오탐이 나지 않아야 한다.
    assert story_llm._find_missing_keys(_load("spec_valid.json")) == []


def test_block_of_maps_event_ending_paths() -> None:
    assert story_llm._block_of("main_events[0].key_sentence") == "main_events"
    assert story_llm._block_of("endings") == "endings"
    assert story_llm._block_of("endings[2].min_turns") == "endings"


async def test_compile_story_falls_back_to_empty_endings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 재호출 후에도 엔딩이 온전한 3개가 아니면 502가 아니라 빈 배열로 폴백해 200으로 반환한다.
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        data["endings"] = data["endings"][:2]  # 매번 2개 → 재호출로도 3개 못 채움
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert isinstance(res, StoryCompileResponse)
    assert res.story_endings == []  # 엔딩만 폴백
    assert 3 <= len(res.story_main_events) <= 5  # 스토리 본체는 살아남음


async def test_compile_story_keeps_valid_endings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 정상 엔딩 3개는 재호출·폴백 없이 그대로 응답에 실린다.
    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _load("spec_valid.json"), story_llm.LlmUsage("m", 100, 200, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert len(res.story_endings) == 3
    assert res.story_endings[0].name
    assert res.meta.retry_count == 0


async def test_compile_story_502_when_endings_incomplete_and_other_field_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 엔딩이 미완성이어도, 엔딩 외 필수 필드가 비면 여전히 502여야 한다
    # (엔딩 폴백이 진짜 실패를 가리지 않는지 — 마스킹 방지).
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        data["endings"] = data["endings"][:2]  # 엔딩 미완성
        data["start"]["prologue"] = ""  # 엔딩 외 필수 누락
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    with pytest.raises(HTTPException) as exc:
        await story_llm.compile_story(_request())
    assert exc.value.status_code == 502


async def test_compile_story_keeps_numeric_string_min_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # min_turns가 숫자 문자열("15")이면 재호출·폴백 없이 정수로 실려야 한다(_is_valid_min_turns↔Pydantic 일치).
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        for e in data["endings"]:
            e["min_turns"] = "15"
        return data, story_llm.LlmUsage("m", 100, 200, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert res.meta.retry_count == 0  # 재호출 없음
    assert len(res.story_endings) == 3
    assert res.story_endings[0].min_turns == 15  # 정수로 강제


async def test_compile_story_falls_back_when_min_turns_uncoercible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 정수로 못 바꾸는 min_turns가 재호출로도 안 고쳐지면 엔딩만 빈 배열로 폴백해 200을 반환한다
    # (_endings_incomplete의 파싱 실패 분기 커버).
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        data["endings"][0]["min_turns"] = "열다섯"  # 매번 비정수
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert res.story_endings == []  # 엔딩만 폴백
    assert 3 <= len(res.story_main_events) <= 5  # 스토리 본체는 살아남음


async def test_compile_story_falls_back_when_min_turns_below_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 하한 미달(0) min_turns가 재호출로도 안 고쳐지면 502가 아니라 엔딩만 빈 배열로 폴백한다
    # (ge=1 강제가 502를 유발하지 않고 폴백으로 흐르는지 — 얕은 검사↔Pydantic 정합).
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        data["endings"][0]["min_turns"] = 0  # 매번 하한 미달
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())
    assert res.story_endings == []  # 엔딩만 폴백
    assert 3 <= len(res.story_main_events) <= 5  # 스토리 본체는 살아남음


async def test_compile_story_falls_back_on_unicode_digit_min_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # int()가 못 바꾸는 유니코드 숫자('²') min_turns는 500이 아니라 엔딩만 빈 배열로 폴백한다
    # (isdigit()의 헐거움이 ValueError를 흘려 500이 나던 회귀 방지 — Gemini 리뷰).
    async def fake_complete(system: str, user: str, **_kwargs: object):
        data = _load("spec_valid.json")
        data["endings"][0]["min_turns"] = "²"  # 위첨자 2
        return data, story_llm.LlmUsage("m", 1, 1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    res = await story_llm.compile_story(_request())  # 500 없이 정상 반환
    assert res.story_endings == []  # 엔딩만 폴백
    assert 3 <= len(res.story_main_events) <= 5  # 스토리 본체는 살아남음
