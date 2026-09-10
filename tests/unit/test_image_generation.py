"""이미지 생성 통로 단위 테스트(KNK-938).

외부 호출(OpenAI SDK)을 대체해 성공·시간 초과·API 오류·프롬프트 거부를 검증한다.
"""

import base64
from contextlib import contextmanager
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
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test prompt")
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
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test", timeout=5.0)
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
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test")
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
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="bad prompt")
    with pytest.raises(ImageBadRequest):
        await openai_api.generate(req)


async def test_openai_generate_empty_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """응답에 이미지 데이터가 없으면 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[_FakeImageData(b64_json=None)]))
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test")
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
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test")
    with pytest.raises(ImageGenerationError, match="데이터가 없습니다"):
        await openai_api.generate(req)


async def test_openai_generate_null_data_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """data 첫 항목이 null이어도 AttributeError가 아니라 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[None]))
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test")
    with pytest.raises(ImageGenerationError, match="데이터가 없습니다"):
        await openai_api.generate(req)


async def test_openai_generate_non_string_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """b64_json이 문자열이 아니어도 TypeError가 아니라 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[_FakeImageData(b64_json=123)]))
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test")
    with pytest.raises(ImageGenerationError, match="문자열이 아닙니다"):
        await openai_api.generate(req)


async def test_openai_generate_malformed_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """base64가 깨져 있으면 binascii.Error가 아니라 ImageGenerationError."""
    _mock_client(monkeypatch, response=_FakeResponse(data=[_FakeImageData(b64_json="!!!not-base64!!!")]))
    req = ImageRequest(model="gpt-image-2-low", purpose="character", prompt="test")
    with pytest.raises(ImageGenerationError, match="base64"):
        await openai_api.generate(req)


# ── 공개 함수(generate_image) 테스트 ─────────────────────────────────────────

async def test_generate_image_routes_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_image()가 모델 이름을 보고 OpenAI 어댑터로 분기한다."""
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-low")
    monkeypatch.setattr(settings, "image_timeout", 30.0)
    _mock_client(monkeypatch)

    result = await generate_image("test prompt", purpose="character")
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

    await generate_image("test prompt", purpose="character")
    assert mock.images.generate.call_args.kwargs["size"] == "512x512"


async def test_generate_image_passes_explicit_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """size를 명시하면 IMAGE_SIZE 대신 그 값이 어댑터까지 전달된다(썸네일 세로 크기, KNK-1047).

    썸네일 상수와도 설정값과도 다른 값을 넣어, 어느 쪽을 박아 넣어도 잡히게 한다.
    """
    from src.core.config import settings
    monkeypatch.setattr(settings, "image_model", "gpt-image-2-low")
    monkeypatch.setattr(settings, "image_size", "1024x768")
    mock = _mock_client(monkeypatch)

    await generate_image("test prompt", purpose="character", size="640x960")
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
        await generate_image("test prompt", purpose="character", size=bad_size)
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
        await generate_image("test", purpose="character")


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


# ── Langfuse 관측 배선(KNK-1240) ─────────────────────────────────────────────
# 자동 계측이 images.generate를 덮지 않으므로 어댑터가 observe_generation으로 손수 기록한다.
# 실제 Langfuse 전송 없이(무과금) "무엇을 어디에 기록하는가"만 고정한다.


@dataclass
class _FakeInputTokensDetails:
    text_tokens: int = 7
    image_tokens: int = 0


@dataclass
class _FakeOutputTokensDetails:
    image_tokens: int = 500
    text_tokens: int = 0


@dataclass
class _FakeUsage:
    input_tokens: int = 7
    output_tokens: int = 500
    total_tokens: int = 507
    input_tokens_details: _FakeInputTokensDetails | None = None
    output_tokens_details: _FakeOutputTokensDetails | None = None

    def __post_init__(self):
        if self.input_tokens_details is None:
            self.input_tokens_details = _FakeInputTokensDetails()


@dataclass
class _FakeResponseWithUsage(_FakeResponse):
    usage: _FakeUsage | None = None


class _Recorder:
    """observe_generation 대역 — 시작 인자와 finish 인자, 블록 예외를 기록한다."""

    def __init__(self) -> None:
        self.started: dict = {}
        self.finished: dict | None = None  # finish로 실린 값의 합(여러 번 불려도 누적)
        self.exc: BaseException | None = None

    def finish(self, **kwargs) -> None:
        self.finished = {**(self.finished or {}), **kwargs}


def _mock_observation(monkeypatch) -> _Recorder:
    rec = _Recorder()

    @contextmanager
    def fake_observe_generation(name, **kwargs):
        rec.started = {"name": name, **kwargs}
        try:
            yield rec
        except BaseException as exc:
            rec.exc = exc
            raise

    monkeypatch.setattr(openai_api, "observe_generation", fake_observe_generation)
    return rec


async def test_openai_generate_records_observation_with_usage(monkeypatch) -> None:
    """성공 시: 이름은 용도별, 모델·크기·화질·출력 형식은 시작에, 출력 요약(바이너리 아님)과
    표준+세부 usage 키는 finish에 실린다. 세부 키(input_text·input_image·output_image)가
    있어야 텍스트·이미지 단가가 다른 gpt-image 계열의 비용이 맞게 계산된다."""
    rec = _mock_observation(monkeypatch)
    _mock_client(
        monkeypatch,
        response=_FakeResponseWithUsage(
            usage=_FakeUsage(
                input_tokens_details=_FakeInputTokensDetails(text_tokens=7, image_tokens=0),
                output_tokens_details=_FakeOutputTokensDetails(image_tokens=500, text_tokens=0),
            )
        ),
    )
    req = ImageRequest(
        model="gpt-image-2-low", purpose="thumbnail", prompt="test prompt", size="768x1024", quality="low"
    )
    await openai_api.generate(req)

    assert rec.started == {
        "name": "이미지 생성:썸네일",
        "model": "gpt-image-2-low",
        "model_parameters": {"size": "768x1024", "quality": "low", "output_format": "webp"},
        "input_data": "test prompt",
    }
    assert rec.finished == {
        "output": {"format": "webp", "bytes": len(_FAKE_PNG)},
        "usage_details": {
            "input": 7,
            "output": 500,
            "total": 507,
            "input_text": 7,
            "input_image": 0,
            "output_text": 0,
            "output_image": 500,
        },
    }
    assert rec.exc is None


async def test_openai_generate_observation_name_for_character(monkeypatch) -> None:
    rec = _mock_observation(monkeypatch)
    _mock_client(monkeypatch)
    await openai_api.generate(ImageRequest(model="gpt-image-2-low", purpose="character", prompt="p"))
    assert rec.started["name"] == "이미지 생성:인물"


async def test_openai_generate_observation_unknown_purpose_is_visible(monkeypatch) -> None:
    """모르는 용도는 조용히 인물로 섞이지 않고 값 그대로 이름에 드러난다."""
    rec = _mock_observation(monkeypatch)
    _mock_client(monkeypatch)
    await openai_api.generate(ImageRequest(model="gpt-image-2-low", purpose="banner", prompt="p"))
    assert rec.started["name"] == "이미지 생성:banner"


async def test_openai_generate_records_without_usage(monkeypatch) -> None:
    """usage가 없는 응답(문서상 gpt-image 계열만 usage 제공)도 죽지 않고 출력 요약만 기록한다."""
    rec = _mock_observation(monkeypatch)
    _mock_client(monkeypatch, response=_FakeResponseWithUsage(usage=None))
    await openai_api.generate(ImageRequest(model="gpt-image-2-low", purpose="character", prompt="p"))
    assert rec.finished == {
        "output": {"format": "webp", "bytes": len(_FAKE_PNG)},
        "usage_details": None,
    }


async def test_openai_generate_records_partial_usage(monkeypatch) -> None:
    """세부 필드가 빠진 usage는 있는 값만 싣는다(output_tokens_details=None)."""
    rec = _mock_observation(monkeypatch)
    _mock_client(
        monkeypatch,
        response=_FakeResponseWithUsage(usage=_FakeUsage(output_tokens_details=None)),
    )
    await openai_api.generate(ImageRequest(model="gpt-image-2-low", purpose="character", prompt="p"))
    assert rec.finished["usage_details"] == {
        "input": 7,
        "output": 500,
        "total": 507,
        "input_text": 7,
        "input_image": 0,
    }


async def test_openai_generate_failure_leaves_observation_block_with_neutral_exception(
    monkeypatch,
) -> None:
    """공급자 실패는 관측 블록을 공급자 중립 예외(ImageTimeout 등)로 나간다 — 관측에는 그
    타입 이름이 ERROR로 남고(observe_generation 계약), 응답이 없으니 finish도 불리지 않는다."""
    from openai import APITimeoutError
    import httpx

    rec = _mock_observation(monkeypatch)
    _mock_client(
        monkeypatch,
        side_effect=APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")),
    )
    with pytest.raises(ImageTimeout):
        await openai_api.generate(ImageRequest(model="gpt-image-2-low", purpose="character", prompt="p"))
    assert isinstance(rec.exc, ImageTimeout)
    assert rec.finished is None


async def test_openai_generate_parse_failure_keeps_usage_and_marks_observation(monkeypatch) -> None:
    """응답 해석 실패(빈 데이터)도 관측 블록 안에서 나가 ERROR로 남는다. 단 **usage는 이미
    기록돼 있어야 한다** — 응답이 온 시점에 과금은 일어났으므로, 해석 실패로 원가가 빠지면
    이 티켓의 목적(원가 집계)이 깨진다(자체 리뷰)."""
    rec = _mock_observation(monkeypatch)
    _mock_client(monkeypatch, response=_FakeResponseWithUsage(data=[], usage=_FakeUsage()))
    with pytest.raises(ImageGenerationError):
        await openai_api.generate(ImageRequest(model="gpt-image-2-low", purpose="character", prompt="p"))
    assert isinstance(rec.exc, ImageGenerationError)
    assert rec.finished == {"usage_details": {"input": 7, "output": 500, "total": 507, "input_text": 7, "input_image": 0}}


async def test_generate_image_requires_purpose() -> None:
    """purpose에 기본값이 없다 — 새 호출부가 인물로 잘못 집계되지 않게 호출 시점에 드러낸다."""
    with pytest.raises(TypeError):
        await generate_image("test prompt")  # type: ignore[call-arg]
