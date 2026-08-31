"""컴파일 인물별 이미지 병렬 생성 테스트(KNK-939).

프롬프트 조립, 병렬 생성, 실패 격리를 검증한다.
"""

import asyncio

import pytest

from src.schemas.story_compile import CharacterSetting
from src.services.image.base import ImageGenerationError, ImageResult, ImageTimeout
from src.services.image.prompt import _load_template, build_image_prompt
from src.services.image.generate_characters import (
    CharacterImageResult,
    generate_character_images,
)


# ── 픽스처 ────────────────────────────────────────────────────────────────────

def _char(**overrides) -> CharacterSetting:
    defaults = {
        "name": "레이",
        "gender": "남성",
        "personality": "충직한 원칙주의자.",
        "tone": "직설적인 말투.",
        "motivation": "진실 규명.",
        "attitude_to_user": "신뢰하는 전우.",
        "age": "20대 후반",
        "body": "건장한",
        "face": "각진 턱선, 굳은 표정",
        "hair": "짧은 단발",
        "outfit": "은색 판금 흉갑",
        "visual_identity": "왼쪽 관자놀이의 칼자국",
    }
    defaults.update(overrides)
    return CharacterSetting(**defaults)


_GENRE = ["다크 판타지"]
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


# ── 프롬프트 조립 테스트 ──────────────────────────────────────────────────────

def test_build_image_prompt_fills_character_block() -> None:
    """외형 필드가 <character> 블록에 올바르게 들어간다."""
    prompt = build_image_prompt(_char(), _GENRE)
    assert prompt is not None
    assert "<genre>다크 판타지</genre>" in prompt
    assert "<gender>남성</gender>" in prompt
    assert "<age>20대 후반</age>" in prompt
    assert "<body>건장한</body>" in prompt
    assert "<face>각진 턱선, 굳은 표정</face>" in prompt
    assert "<hair>짧은 단발</hair>" in prompt
    assert "<outfit>은색 판금 흉갑</outfit>" in prompt
    assert "<visual_identity>왼쪽 관자놀이의 칼자국</visual_identity>" in prompt


def test_build_image_prompt_multiple_genres() -> None:
    """장르가 여러 개면 쉼표로 합쳐진다."""
    prompt = build_image_prompt(_char(), ["무협", "회귀"])
    assert "<genre>무협, 회귀</genre>" in prompt


def test_build_image_prompt_returns_none_when_appearance_missing() -> None:
    """외형 필드가 비어 있으면 None을 반환한다."""
    assert build_image_prompt(_char(face=""), _GENRE) is None
    assert build_image_prompt(_char(age=""), _GENRE) is None
    assert build_image_prompt(_char(visual_identity=""), _GENRE) is None


def test_build_image_prompt_returns_none_when_appearance_is_whitespace() -> None:
    """외형 필드가 공백뿐이어도 None을 반환한다."""
    assert build_image_prompt(_char(hair="   "), _GENRE) is None


def test_build_image_prompt_has_fixed_blocks() -> None:
    """고정 블록(task, composition 등)이 프롬프트에 포함된다."""
    prompt = build_image_prompt(_char(), _GENRE)
    assert "<task>" in prompt
    assert "<composition>" in prompt
    assert "<visual_style>" in prompt
    assert "<quality_requirements>" in prompt
    assert "<output>" in prompt


# ── 템플릿 로더 테스트 (KNK-1048: 경로 인자화) ───────────────────────────────

def test_load_template_extracts_image_prompt_block(tmp_path) -> None:
    """frontmatter·제목을 건너뛰고 <image_prompt> 블록만 꺼낸다."""
    path = tmp_path / "T.md"
    path.write_text(
        "---\nversion: 1\n---\n# 제목\n설명\n<image_prompt>\n<genre>{{genre}}</genre>\n</image_prompt>\n",
        encoding="utf-8",
    )
    body = _load_template(path)
    assert body.startswith("<image_prompt>")
    assert body.endswith("</image_prompt>")
    assert "version:" not in body


def test_load_template_missing_file_raises(tmp_path) -> None:
    """템플릿 파일이 없으면 경로를 담은 RuntimeError."""
    path = tmp_path / "MISSING.md"
    with pytest.raises(RuntimeError, match="찾을 수 없습니다"):
        _load_template(path)


def test_load_template_missing_block_raises(tmp_path) -> None:
    """<image_prompt> 블록이 없으면 RuntimeError."""
    path = tmp_path / "T.md"
    path.write_text("---\nversion: 1\n---\n본문만 있음\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="<image_prompt> 블록이 없습니다"):
        _load_template(path)


# ── 병렬 생성 테스트 ──────────────────────────────────────────────────────────

async def test_generate_all_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """인물 3명 전원 성공."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt: str):
        return ImageResult(image_bytes=_FAKE_PNG, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", gender="여성"), _char(name="칸")]
    results = await generate_character_images(chars, _GENRE)

    assert len(results) == 3
    assert all(r.image is not None for r in results)
    assert [r.name for r in results] == ["레이", "세린", "칸"]


async def test_generate_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """한 인물 실패 시 나머지는 정상 반환된다."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt: str):
        # asyncio.gather 실행 순서에 의존하지 않도록 프롬프트 내용으로 실패를 결정한다.
        # 세린만 gender="여성"이라 프롬프트에 <gender>여성</gender>이 들어간다.
        if "<gender>여성</gender>" in prompt:
            raise ImageTimeout("시간 초과")
        return ImageResult(image_bytes=_FAKE_PNG, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", gender="여성"), _char(name="칸")]
    results = await generate_character_images(chars, _GENRE)

    assert len(results) == 3
    successes = [r for r in results if r.image is not None]
    failures = [r for r in results if r.image is None]
    assert len(successes) == 2
    assert len(failures) == 1
    assert failures[0].error is not None


async def test_generate_skips_missing_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    """외형 필드가 비어서 프롬프트를 못 만드는 인물은 건너뛴다."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt: str):
        return ImageResult(image_bytes=_FAKE_PNG, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", face="")]  # 세린은 외형 부족
    results = await generate_character_images(chars, _GENRE)

    assert len(results) == 2
    assert results[0].image is not None  # 레이는 성공
    assert results[1].image is None  # 세린은 건너뜀
    assert "외형 필드 부족" in results[1].error


async def test_generate_survives_malformed_provider_response(monkeypatch) -> None:
    """공급자 응답이 깨진 인물만 실패하고 나머지 인물 이미지는 살아남는다(PR #92 리뷰).

    generate_image를 가짜로 바꾸지 않고 어댑터(openai_api)까지 실제로 태운다 —
    어댑터가 응답 해석 실패를 ImageGenerationError로 접지 않으면 gather가 터져
    성공한 인물까지 빈 배열이 되는 회귀를 여기서 잡는다.
    """
    import base64
    from dataclasses import dataclass
    from unittest.mock import AsyncMock

    from src.core.config import settings
    from src.services.image import openai_api

    @dataclass
    class _Data:
        b64_json: str | None

    @dataclass
    class _Resp:
        data: list

    good = _Resp(data=[_Data(b64_json=base64.b64encode(_FAKE_PNG).decode())])
    broken = _Resp(data=[_Data(b64_json="!!!not-base64!!!")])

    async def fake_generate(**kwargs):
        # 세린만 gender="여성"이라 프롬프트로 구분한다(실행 순서 비의존).
        return broken if "<gender>여성</gender>" in kwargs["prompt"] else good

    mock = AsyncMock()
    mock.images.generate = AsyncMock(side_effect=fake_generate)
    monkeypatch.setattr(openai_api, "_client", lambda *a, **kw: mock)
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-2026-04-21")

    chars = [_char(name="레이"), _char(name="세린", gender="여성"), _char(name="칸")]
    results = await generate_character_images(chars, _GENRE)

    assert [r.name for r in results] == ["레이", "세린", "칸"]
    assert results[0].image is not None
    assert results[1].image is None and "base64" in results[1].error
    assert results[2].image is not None


async def test_generate_empty_list() -> None:
    """빈 인물 목록은 빈 결과를 반환한다."""
    results = await generate_character_images([], _GENRE)
    assert results == []


async def test_generate_all_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """전원 실패해도 예외가 나지 않고 결과가 돌아온다."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt: str):
        raise ImageGenerationError("전부 실패")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="칸")]
    results = await generate_character_images(chars, _GENRE)

    assert len(results) == 2
    assert all(r.image is None for r in results)
    assert all(r.error is not None for r in results)


# ── 실패는 Sentry로 보고한다 (PR #92 리뷰) ────────────────────────────────────
# 이미지 실패는 인물만 비우고 컴파일은 살리므로, 여기서 보고하지 않으면 시간 초과·429·거부가
# 로그에만 남아 아무도 모른다. 외형 부족은 공급자 실패가 아니라 보고하지 않는다.

def _capture_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import src.services.image.generate_characters as gen_mod

    calls: list[dict] = []

    def _record(exc, **kwargs):
        calls.append({"exc": exc, **kwargs})

    monkeypatch.setattr(gen_mod, "capture_ai_exception", _record)
    return calls


async def test_provider_failure_is_reported_to_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.image.generate_characters as gen_mod

    calls = _capture_recorder(monkeypatch)

    async def fake_generate(prompt: str):
        if "<gender>여성</gender>" in prompt:
            raise ImageTimeout("시간 초과")
        return ImageResult(image_bytes=_FAKE_PNG, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", gender="여성")]
    await generate_character_images(chars, _GENRE)

    assert len(calls) == 1  # 실패한 세린 1건만
    call = calls[0]
    assert isinstance(call["exc"], ImageTimeout)
    assert call["feature"] == "character_image_generation"
    assert call["provider"] == "openai"
    assert "CHARACTER_IMAGE" in call["prompt_versions"]
    assert call["latency_ms"] >= 0


async def test_missing_appearance_is_not_reported_to_sentry(monkeypatch) -> None:
    import src.services.image.generate_characters as gen_mod

    calls = _capture_recorder(monkeypatch)

    async def fake_generate(prompt: str):
        return ImageResult(image_bytes=_FAKE_PNG, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    await generate_character_images([_char(name="세린", face="")], _GENRE)

    assert calls == []
