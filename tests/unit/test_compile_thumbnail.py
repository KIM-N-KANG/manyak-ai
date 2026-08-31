"""컴파일 → 썸네일(표지) 연결 테스트(KNK-1050).

generate_thumbnail_image(공급자 호출·Sentry 보고), _generate_thumbnail_image_safe(응답 객체 변환·
실패 코드·예외 접기), compile_story의 동시 실행과 meta 버전을 검증한다.
실제 표지 품질은 유닛으로 검증되지 않는다 — 라이브 실측은 별도.
"""

import asyncio
import base64
import json
from pathlib import Path

import pytest

from src.schemas.story_compile import CharacterSetting, StoryCompileResponse, ThumbnailImageOut
from src.services import story_llm
from src.services.image import THUMBNAIL_IMAGE_SIZE
from src.services.image.base import ImageBadRequest, ImageGenerationError, ImageRateLimited, ImageResult, ImageTimeout
from src.services.image import generate_thumbnail as thumb_mod
from src.services.image.generate_thumbnail import ThumbnailImageResult, generate_thumbnail_image
from src.services.story_llm import _generate_thumbnail_image_safe

_FIXTURES = Path(__file__).parent / "fixtures"
_FAKE_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 50
_GENRE = ["다크 판타지"]


def _char(name: str = "레이", **overrides) -> CharacterSetting:
    defaults = {
        "name": name,
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


# ── generate_thumbnail_image ──────────────────────────────────────────────────


async def test_generate_thumbnail_calls_image_with_portrait_size(monkeypatch) -> None:
    """썸네일 호출은 인물 이미지 크기가 아니라 THUMBNAIL_IMAGE_SIZE(768x1024)로 나간다."""
    seen: dict = {}

    async def fake_generate(prompt, *, size=None):
        seen["prompt"] = prompt
        seen["size"] = size
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(thumb_mod, "generate_image", fake_generate)

    result = await generate_thumbnail_image([_char()], _GENRE)

    assert result.image is not None and result.error is None
    assert seen["size"] == THUMBNAIL_IMAGE_SIZE == "768x1024"
    assert "<genre>다크 판타지</genre>" in seen["prompt"]
    assert "<character>" in seen["prompt"]


async def test_generate_thumbnail_reports_provider_failure_to_sentry(monkeypatch) -> None:
    """공급자 실패는 예외 대신 error로 돌아오고, Sentry에 썸네일 태그로 보고된다."""
    calls: list[dict] = []

    def _record(exc, **kwargs):
        calls.append({"exc": exc, **kwargs})

    async def fake_generate(prompt, *, size=None):
        raise ImageTimeout("이미지 생성 시간 초과 (60초)")

    monkeypatch.setattr(thumb_mod, "capture_ai_exception", _record)
    monkeypatch.setattr(thumb_mod, "generate_image", fake_generate)

    result = await generate_thumbnail_image([_char()], _GENRE)

    assert result.image is None
    assert "시간 초과" in result.error
    assert len(calls) == 1
    assert calls[0]["feature"] == "thumbnail_image_generation"
    assert calls[0]["provider"] == "openai"
    assert "THUMBNAIL_IMAGE" in calls[0]["prompt_versions"]
    assert calls[0]["retry_count"] == 0


# ── _generate_thumbnail_image_safe ───────────────────────────────────────────


async def test_thumbnail_safe_returns_base64_on_success(monkeypatch) -> None:
    async def fake_thumb(characters, genre_tags):
        return ThumbnailImageResult(image=ImageResult(image_bytes=_FAKE_WEBP, model="t", provider="openai"))

    monkeypatch.setattr(thumb_mod, "generate_thumbnail_image", fake_thumb)

    out = await _generate_thumbnail_image_safe([_char()], _GENRE)

    assert isinstance(out, ThumbnailImageOut)
    assert out.image_name == "썸네일_기본"
    assert out.content_type == "image/webp"
    assert out.error is None
    assert base64.b64decode(out.image_base64) == _FAKE_WEBP


@pytest.mark.parametrize(
    ("provider_error", "code"),
    [
        (ImageTimeout("이미지 생성 시간 초과 (60초)"), "timeout"),
        (ImageRateLimited("이미지 생성 속도 제한: 429"), "rate_limited"),
        (ImageBadRequest("이미지 생성 요청 거부: safety"), "rejected"),
        (ImageGenerationError("이미지 생성 실패: 알 수 없음"), "generation_failed"),
    ],
)
async def test_thumbnail_safe_maps_failure_to_stable_code(monkeypatch, provider_error, code) -> None:
    """공급자 원문은 응답에 나가지 않고 안정적인 코드 4종 중 하나로 접힌다."""
    async def fake_generate(prompt, *, size=None):
        raise provider_error

    monkeypatch.setattr(thumb_mod, "generate_image", fake_generate)
    monkeypatch.setattr(thumb_mod, "capture_ai_exception", lambda *a, **k: None)

    out = await _generate_thumbnail_image_safe([_char()], _GENRE)

    assert out.image_base64 is None
    assert out.error == code
    assert out.image_name == "썸네일_기본"
    assert out.content_type == "image/webp"


async def test_thumbnail_safe_folds_unexpected_exception(monkeypatch) -> None:
    """썸네일 로직 자체의 예외도 삼켜 generation_failed 객체로 돌려주고 unexpected_error로 보고한다."""
    calls: list[dict] = []

    def _record(exc, **kwargs):
        calls.append({"exc": exc, **kwargs})

    async def boom(characters, genre_tags):
        raise RuntimeError("예상치 못한 오류")

    monkeypatch.setattr(story_llm, "capture_ai_exception", _record)
    monkeypatch.setattr(thumb_mod, "generate_thumbnail_image", boom)

    out = await _generate_thumbnail_image_safe([_char()], _GENRE)

    assert out.image_base64 is None
    assert out.error == "generation_failed"
    assert len(calls) == 1
    assert isinstance(calls[0]["exc"], RuntimeError)
    assert calls[0]["feature"] == "thumbnail_image_generation"
    assert calls[0]["error_code"] == "unexpected_error"
    # 공급자 실패 경로와 같은 관측값이 붙는다(Codex 리뷰 3)
    assert calls[0]["retry_count"] == 0
    assert isinstance(calls[0]["latency_ms"], int)


# ── 스키마 ────────────────────────────────────────────────────────────────────


def test_thumbnail_image_out_rejects_contract_violations() -> None:
    """이름·형식·코드는 계약값만, 성공/실패는 둘 중 하나만 — 어긋난 값은 만들어질 때 막힌다."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ThumbnailImageOut(image_name="wrong", error="timeout")
    with pytest.raises(ValidationError):
        ThumbnailImageOut(content_type="image/png", error="timeout")
    with pytest.raises(ValidationError):
        ThumbnailImageOut(error="appearance_missing")
    with pytest.raises(ValidationError):  # 그림도 있고 실패 사유도 있음
        ThumbnailImageOut(image_base64="abc", error="timeout")
    with pytest.raises(ValidationError):  # 그림도 없고 실패 사유도 없음
        ThumbnailImageOut()


def test_compile_response_thumbnail_is_required() -> None:
    """thumbnail_image는 기본값 없는 필수 필드다 — OpenAPI에 required로 실리고 null도 막힌다."""
    from pydantic import ValidationError

    from src.main import app

    assert StoryCompileResponse.model_fields["thumbnail_image"].is_required()
    schema = app.openapi()["components"]["schemas"]["StoryCompileResponse"]
    assert "thumbnail_image" in schema["required"]

    full = StoryCompileResponse.model_validate(
        spec_to_response_dict() | {"thumbnail_image": {"image_name": "썸네일_기본", "error": "timeout"}}
    )
    assert full.thumbnail_image.error == "timeout"
    with pytest.raises(ValidationError):
        StoryCompileResponse.model_validate(spec_to_response_dict() | {"thumbnail_image": None})
    with pytest.raises(ValidationError):
        StoryCompileResponse.model_validate(spec_to_response_dict())


def spec_to_response_dict() -> dict:
    """thumbnail_image를 뺀 컴파일 응답 dict(필수 필드 검증용)."""
    from src.schemas.story_compile import StorySpec
    from src.services.story_compile_render import spec_to_response

    res = spec_to_response(StorySpec(**_spec()), thumbnail_image=ThumbnailImageOut(error="generation_failed"))
    data = res.model_dump()
    del data["thumbnail_image"]
    return data


# ── compile_story 연결 ────────────────────────────────────────────────────────


def _request():
    from src.schemas.story_compile import StoryCompileRequest

    return StoryCompileRequest(
        selected_storyline="x",
        additional_info="",
        genre_tags=["다크 판타지"],
        protagonist={"name": "카일", "gender": "MALE", "features": ["신중한"]},
        supporting_characters=[{"name": "레이", "gender": "MALE", "features": ["충직한"]}],
    )


def _spec() -> dict:
    return json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8"))


async def test_compile_story_includes_thumbnail(monkeypatch) -> None:
    """compile_story()가 표지를 만들어 응답 thumbnail_image에 싣고, meta에 버전을 기록한다."""
    spec = _spec()

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    async def fake_generate(prompt, *, size=None):
        assert size == THUMBNAIL_IMAGE_SIZE
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", _generate_thumbnail_image_safe)
    monkeypatch.setattr(thumb_mod, "generate_image", fake_generate)

    response = await story_llm.compile_story(_request())

    assert isinstance(response, StoryCompileResponse)
    assert response.stories.title == "잿빛 왕관"
    assert response.thumbnail_image.image_name == "썸네일_기본"
    assert response.thumbnail_image.error is None
    assert base64.b64decode(response.thumbnail_image.image_base64) == _FAKE_WEBP
    assert response.meta.prompt_versions["THUMBNAIL_IMAGE"] >= 1
    assert list(response.meta.prompt_versions) == ["COMPILE", "CHARACTER_IMAGE", "THUMBNAIL_IMAGE"]


async def test_compile_story_thumbnail_failure_keeps_200_and_character_images(monkeypatch) -> None:
    """표지가 실패해도 컴파일은 성공하고 인물 이미지는 그대로다(격리)."""
    spec = _spec()

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    async def fake_images(characters, genre_tags):
        from src.schemas.story_compile import CharacterImageOut

        return [CharacterImageOut(name="레이", image_name="레이_기본", image_base64="AAAA")]

    async def fake_generate(prompt, *, size=None):
        raise ImageBadRequest("이미지 생성 요청 거부: size")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", fake_images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", _generate_thumbnail_image_safe)
    monkeypatch.setattr(thumb_mod, "generate_image", fake_generate)
    monkeypatch.setattr(thumb_mod, "capture_ai_exception", lambda *a, **k: None)

    response = await story_llm.compile_story(_request())

    assert response.thumbnail_image.image_base64 is None
    assert response.thumbnail_image.error == "rejected"
    assert response.character_images[0].image_base64 == "AAAA"


async def test_compile_story_character_failure_keeps_thumbnail(monkeypatch) -> None:
    """인물 이미지가 통째로 실패(빈 배열)해도 표지는 만들어진다(격리, 반대 방향)."""
    spec = _spec()

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    async def fake_images(characters, genre_tags):
        return []

    async def fake_thumb(characters, genre_tags):
        return ThumbnailImageOut(image_name="썸네일_기본", image_base64="QkJCQg==")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", fake_images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", fake_thumb)

    response = await story_llm.compile_story(_request())

    assert response.character_images == []
    assert response.thumbnail_image.image_base64 == "QkJCQg=="


async def test_compile_story_runs_images_and_thumbnail_concurrently(monkeypatch) -> None:
    """인물 이미지와 표지가 순서대로가 아니라 동시에 돈다.

    각 가짜가 상대가 시작하기를 기다린다 — 순서대로 돌면 서로 기다리다 멈추므로
    wait_for 시간 초과로 드러난다.
    """
    spec = _spec()
    images_started = asyncio.Event()
    thumbnail_started = asyncio.Event()

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    async def fake_images(characters, genre_tags):
        images_started.set()
        await thumbnail_started.wait()
        return []

    async def fake_thumb(characters, genre_tags):
        thumbnail_started.set()
        await images_started.wait()
        return ThumbnailImageOut(image_name="썸네일_기본", image_base64="QQ==")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", fake_images)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", fake_thumb)

    response = await asyncio.wait_for(story_llm.compile_story(_request()), timeout=2)

    assert response.thumbnail_image.image_base64 == "QQ=="
