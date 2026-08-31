"""이미지 생성 통로 단위 테스트(KNK-938).

외부 호출(OpenAI SDK)을 대체해 성공·시간 초과·API 오류·프롬프트 거부를 검증한다.
"""

import base64
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from src.services.image import (
    THUMBNAIL_IMAGE_SIZE,
    generate_image,
    validate_startup,
    ImageGenerationError,
)
from src.services.image.base import (
    ImageBadRequest,
    ImageRateLimited,
    ImageRequest,
    ImageTimeout,
)
from src.services.image import openai_api


# ── 픽스처 ────────────────────────────────────────────────────────────────────

_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 가짜 PNG 바이너리
_FAKE_B64 = base64.b64encode(_FAKE_PNG).decode()


@dataclass
class _FakeImageData:
    b64_json: str | None = _FAKE_B64


@dataclass
class _FakeResponse:
    data: list = None

    def __post_init__(self):
        if self.data is None:
            self.data = [_FakeImageData()]


def _mock_client(monkeypatch, response=None, side_effect=None):
    """openai_api._client를 가짜 클라이언트로 교체한다."""
    mock = AsyncMock()
    if side_effect:
        mock.images.generate = AsyncMock(side_effect=side_effect)
    else:
        mock.images.generate = AsyncMock(return_value=response or _FakeResponse())
    monkeypatch.setattr(openai_api, "_client", lambda *a, **kw: mock)
    return mock


# ── 어댑터 직접 호출 테스트 ───────────────────────────────────────────────────

async def test_openai_generate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 호출 시 PNG 바이너리와 모델·공급자를 돌려준다."""
    _mock_client(monkeypatch)
    req = ImageRequest(model="gpt-image-2-low", prompt="test prompt")
    result = await openai_api.generate(req)

    assert result.image_bytes == _FAKE_PNG
    assert result.model == "gpt-image-2-low"
    assert result.provider == "openai"


async def test_openai_generate_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """시간 초과 시 ImageTimeout으로 변환된다."""
    from openai import APITimeoutError
    import httpx

    _mock_client(
        monkeypatch,
        side_effect=APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")),
    )
    req = ImageRequest(model="gpt-image-2-low", prompt="test", timeout=5.0)
    with pytest.raises(ImageTimeout):
        await openai_api.generate(req)


def _httpx_response(status_code: int) -> "httpx.Response":
    """테스트용 httpx.Response — request를 붙여야 OpenAI SDK 예외가 안 깨진다."""
    import httpx

    resp = httpx.Response(status_code)
    resp._request = httpx.Request("POST", "https://api.openai.com")
    return resp


async def test_openai_generate_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """속도 제한 시 ImageRateLimited로 변환된다."""
    from openai import RateLimitError

    _mock_client(
        monkeypatch,
        side_effect=RateLimitError(
            message="rate limited",
            response=_httpx_response(429),
            body=None,
        ),
    )
    req = ImageRequest(model="gpt-image-2-low", prompt="test")
    with pytest.raises(ImageRateLimited):
        await openai_api.generate(req)


async def test_openai_generate_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """프롬프트 거부 시 ImageBadRequest로 변환된다."""
    from openai import BadRequestError

    _mock_client(
        monkeypatch,
        side_effect=BadRequestError(
            message="content policy violation",
            response=_httpx_response(400),
            body=None,
        ),
    )
    req = ImageRequest(model="gpt-image-2-low", prompt="bad prompt")
    with pytest.raises(ImageBadRequest):
        await openai_api.generate(req)


async def test_openai_generate_empty_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """응답에 이미지 데이터가 없으면 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[_FakeImageData(b64_json=None)]))
    req = ImageRequest(model="gpt-image-2-low", prompt="test")
    with pytest.raises(ImageGenerationError, match="데이터가 없습니다"):
        await openai_api.generate(req)


# ── 응답 해석 실패도 ImageGenerationError로 접는다 (PR #92 리뷰) ────────────
# 이 예외만 인물 단위 실패로 처리된다. IndexError·binascii.Error가 그대로 새면
# 병렬 생성 전체가 중단돼 성공한 인물 이미지까지 버려진다.

@pytest.mark.parametrize("data", [[], None], ids=["empty_list", "none"])
async def test_openai_generate_missing_data_list(monkeypatch, data) -> None:
    """data 목록 자체가 비어 있어도 IndexError가 아니라 ImageGenerationError."""
    response = _FakeResponse(data=[_FakeImageData()])
    response.data = data
    _mock_client(monkeypatch, response=response)
    req = ImageRequest(model="gpt-image-2-low", prompt="test")
    with pytest.raises(ImageGenerationError, match="데이터가 없습니다"):
        await openai_api.generate(req)


async def test_openai_generate_null_data_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """data 첫 항목이 null이어도 AttributeError가 아니라 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[None]))
    req = ImageRequest(model="gpt-image-2-low", prompt="test")
    with pytest.raises(ImageGenerationError, match="데이터가 없습니다"):
        await openai_api.generate(req)


async def test_openai_generate_non_string_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """b64_json이 문자열이 아니어도 TypeError가 아니라 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[_FakeImageData(b64_json=123)]))
    req = ImageRequest(model="gpt-image-2-low", prompt="test")
    with pytest.raises(ImageGenerationError, match="문자열이 아닙니다"):
        await openai_api.generate(req)


async def test_openai_generate_malformed_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """base64가 깨져 있으면 binascii.Error가 아니라 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[_FakeImageData(b64_json="!!!not-base64!!!")]))
    req = ImageRequest(model="gpt-image-2-low", prompt="test")
    with pytest.raises(ImageGenerationError, match="base64"):
        await openai_api.generate(req)


# ── 공개 함수(generate_image) 테스트 ─────────────────────────────────────────

async def test_generate_image_routes_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_image()가 모델 이름을 보고 OpenAI 어댑터로 분기한다."""
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-low")
    monkeypatch.setattr(settings, "image_timeout", 30.0)
    _mock_client(monkeypatch)

    result = await generate_image("test prompt")
    assert result.image_bytes == _FAKE_PNG
    assert result.provider == "openai"


async def test_generate_image_uses_settings_size_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """size를 주지 않으면 IMAGE_SIZE가 어댑터까지 전달된다.

    기본 설정값(1024x768)이 아닌 값을 넣어, 구현이 값을 박아 넣어도 통과하는 일을 막는다.
    """
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-low")
    monkeypatch.setattr(settings, "image_size", "512x512")
    mock = _mock_client(monkeypatch)

    await generate_image("test prompt")
    assert mock.images.generate.call_args.kwargs["size"] == "512x512"


async def test_generate_image_passes_explicit_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """size를 명시하면 IMAGE_SIZE 대신 그 값이 어댑터까지 전달된다(썸네일 세로 크기, KNK-1047).

    썸네일 상수와도 설정값과도 다른 값을 넣어, 어느 쪽을 박아 넣어도 잡히게 한다.
    """
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-low")
    monkeypatch.setattr(settings, "image_size", "1024x768")
    mock = _mock_client(monkeypatch)

    await generate_image("test prompt", size="640x960")
    assert mock.images.generate.call_args.kwargs["size"] == "640x960"


@pytest.mark.parametrize("bad_size", ["", "wide", "1024", "0x768", "768x1024x1"])
async def test_generate_image_rejects_invalid_explicit_size(monkeypatch, bad_size) -> None:
    """잘못된 size를 명시하면 공급자를 부르기 전에 ImageGenerationError로 거부한다.

    빈 문자열도 설정값으로 대체하지 않고 잘못된 입력으로 본다.
    """
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-low")
    mock = _mock_client(monkeypatch)

    with pytest.raises(ImageGenerationError, match="가로x세로"):
        await generate_image("test prompt", size=bad_size)
    mock.images.generate.assert_not_called()


def test_thumbnail_image_size_is_portrait_3_by_4() -> None:
    """썸네일 크기 상수는 3:4 세로 768x1024이고 IMAGE_SIZE와 같은 형식 검사를 통과한다."""
    from src.services.image import _SIZE_RE

    assert THUMBNAIL_IMAGE_SIZE == "768x1024"
    assert _SIZE_RE.fullmatch(THUMBNAIL_IMAGE_SIZE)


async def test_generate_image_unknown_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """등록되지 않은 모델이면 ImageGenerationError."""
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "unknown-model-9000")

    with pytest.raises(ImageGenerationError, match="등록되지 않았습니다"):
        await generate_image("test")


# ── 모델 등록 테스트 ──────────────────────────────────────────────────────────

def test_registered_models_have_adapters() -> None:
    """등록된 모든 이미지 모델이 유효한 어댑터를 가리킨다."""
    from src.services.image import _MODEL_ADAPTERS
    from src.services.image.base import ADAPTER_OPENAI_IMAGE

    valid_adapters = {ADAPTER_OPENAI_IMAGE}
    for model, adapter in _MODEL_ADAPTERS.items():
        assert adapter in valid_adapters, f"모델 '{model}'의 어댑터 '{adapter}'가 유효하지 않다"


# ── 기동 설정 검사 ────────────────────────────────────────────────────────────

def _valid_image_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "image_model", "gpt-image-2-2026-04-21")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(settings, "openai_api_url", None)
    monkeypatch.setattr(settings, "image_quality", "low")
    monkeypatch.setattr(settings, "image_size", "1024x768")
    monkeypatch.setattr(settings, "image_timeout", 60.0)


def test_image_startup_validation_accepts_valid_settings(monkeypatch) -> None:
    _valid_image_settings(monkeypatch)

    validate_startup()


def test_image_startup_validation_rejects_unknown_model(monkeypatch) -> None:
    from src.core.config import settings

    _valid_image_settings(monkeypatch)
    monkeypatch.setattr(settings, "image_model", "unknown-image-model")

    with pytest.raises(ImageGenerationError, match="IMAGE_MODEL"):
        validate_startup()


@pytest.mark.parametrize(
    ("bad_key", "problem"),
    [
        ("", "비어"),
        (" openai-test-key", "앞뒤 공백"),
        ("openai-test\nkey", "개행"),
        ("openai—test-key", "ASCII"),
    ],
)
def test_image_startup_validation_rejects_bad_key(monkeypatch, bad_key, problem) -> None:
    from src.core.config import settings

    _valid_image_settings(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", bad_key)

    with pytest.raises(ImageGenerationError, match=problem):
        validate_startup()


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("openai_api_url", "not-a-url", "OPENAI_API_URL"),
        ("image_quality", "ultra", "IMAGE_QUALITY"),
        ("image_size", "wide", "IMAGE_SIZE"),
        ("image_timeout", 0.0, "IMAGE_TIMEOUT"),
    ],
)
def test_image_startup_validation_rejects_bad_parameters(
    monkeypatch, field, value, problem
) -> None:
    from src.core.config import settings

    _valid_image_settings(monkeypatch)
    monkeypatch.setattr(settings, field, value)

    with pytest.raises(ImageGenerationError, match=problem):
        validate_startup()
