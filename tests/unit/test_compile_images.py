"""컴파일 → 이미지 연결 테스트(KNK-940).

compile_story()가 이미지를 생성해 base64로 응답에 싣는 흐름을 검증한다.
"""

import base64
import json
from pathlib import Path

import pytest

from src.schemas.story_compile import CharacterImageOut, CharacterSetting, StoryCompileResponse
from src.services.image.base import ImageGenerationError, ImageResult
from src.services.image.generate_characters import CharacterImageResult
from src.services import story_llm
from src.services.story_llm import _generate_character_images_safe

_FIXTURES = Path(__file__).parent / "fixtures"


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
_FAKE_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 50


# ── _generate_character_images_safe 테스트 ────────────────────────────────────


async def test_images_safe_returns_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """성공한 인물의 이미지가 base64로 변환된다."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt):
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", gender="여성")]
    result = await _generate_character_images_safe(chars, _GENRE)

    assert len(result) == 2
    assert result[0].name == "레이"
    assert result[0].image_base64 is not None
    assert result[0].error is None
    # base64 디코딩하면 원본과 같다
    assert base64.b64decode(result[0].image_base64) == _FAKE_WEBP


async def test_images_safe_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """한 인물 실패 시 해당 인물만 error가 채워지고 나머지는 정상."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt):
        # asyncio.gather 실행 순서에 의존하지 않도록 프롬프트 내용으로 실패를 결정한다.
        # 세린만 gender="여성"이라 프롬프트에 <gender>여성</gender>이 들어간다.
        if "<gender>여성</gender>" in prompt:
            raise ImageGenerationError("시간 초과")
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", gender="여성"), _char(name="칸")]
    result = await _generate_character_images_safe(chars, _GENRE)

    assert len(result) == 3
    successes = [r for r in result if r.image_base64 is not None]
    failures = [r for r in result if r.image_base64 is None]
    assert len(successes) == 2
    assert len(failures) == 1
    assert failures[0].name == "세린"
    assert failures[0].error == "timeout"  # 공급자 원문이 아닌 분류된 코드


async def test_images_safe_total_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_character_images가 예외를 던져도 빈 배열로 폴백한다."""
    import src.services.image.generate_characters as gen_mod

    async def boom(characters, genre_tags):
        raise RuntimeError("예상치 못한 오류")

    monkeypatch.setattr(
        "src.services.image.generate_characters.generate_character_images",
        boom,
    )

    chars = [_char(name="레이")]
    result = await _generate_character_images_safe(chars, _GENRE)
    assert result == []


async def test_images_safe_skips_missing_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    """외형 필드가 비어 프롬프트를 못 만드는 인물은 error로 돌아온다."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt):
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이"), _char(name="세린", face="")]
    result = await _generate_character_images_safe(chars, _GENRE)

    assert len(result) == 2
    assert result[0].image_base64 is not None
    assert result[1].image_base64 is None
    assert result[1].error == "appearance_missing"


# ── CharacterImageOut 스키마 테스트 ───────────────────────────────────────────


def test_character_image_out_success() -> None:
    """성공 케이스: image_base64가 있고 error가 None."""
    out = CharacterImageOut(name="레이", image_base64="abc123")
    assert out.name == "레이"
    assert out.image_base64 == "abc123"
    assert out.error is None


def test_character_image_out_failure() -> None:
    """실패 케이스: image_base64가 None이고 error가 있다."""
    out = CharacterImageOut(name="세린", error="시간 초과")
    assert out.image_base64 is None
    assert out.error == "시간 초과"


def test_character_image_out_has_content_type() -> None:
    """content_type이 기본값 image/webp로 설정된다."""
    out = CharacterImageOut(name="레이", image_base64="abc123")
    assert out.content_type == "image/webp"


async def test_images_safe_empty_characters() -> None:
    """인물 0명이면 빈 배열을 반환한다."""
    result = await _generate_character_images_safe([], _GENRE)
    assert result == []


async def test_images_safe_base64_error_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """base64 변환 중 오류가 나도 빈 배열로 폴백한다(try 안에 있으므로)."""
    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt):
        # image_bytes가 None이면 base64.b64encode에서 TypeError가 난다
        return ImageResult(image_bytes=None, model="test", provider="openai")

    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    chars = [_char(name="레이")]
    result = await _generate_character_images_safe(chars, _GENRE)
    assert result == []


# ── compile_story → 이미지 → 응답 전체 배선 테스트 ────────────────────────────


def _request():
    from src.schemas.story_compile import StoryCompileRequest
    return StoryCompileRequest(
        selected_storyline="x",
        additional_info="",
        genre_tags=["다크 판타지"],
        protagonist={"name": "카일", "gender": "MALE", "features": ["신중한"]},
        supporting_characters=[
            {"name": "레이", "gender": "MALE", "features": ["충직한"]},
        ],
    )


async def test_compile_story_includes_character_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_story()가 이미지를 생성해 응답의 character_images에 base64로 싣는다."""
    spec = json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8"))

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    import src.services.image.generate_characters as gen_mod

    async def fake_generate(prompt):
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(gen_mod, "generate_image", fake_generate)

    response = await story_llm.compile_story(_request())

    assert isinstance(response, StoryCompileResponse)
    # 스토리 명세는 정상
    assert response.stories.title == "잿빛 왕관"
    # 이미지가 응답에 실렸다 (fixture 인물 3명)
    assert len(response.character_images) == 3
    for img in response.character_images:
        assert img.image_base64 is not None
        assert img.content_type == "image/webp"
        assert img.error is None
        # base64 디코딩하면 원본과 같다
        assert base64.b64decode(img.image_base64) == _FAKE_WEBP
