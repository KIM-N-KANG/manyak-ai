"""LLM 통로의 이미지 입력 지원 테스트(KNK-1359).

세 가지를 고정한다. 이미지 조각의 모양(OpenAI 호환 data URL), 등록부의 이미지 지원 표시,
그리고 **이미지를 못 받는 모델에 이미지 요청이 가지 않는다**는 통로·어댑터 검사.
마지막이 핵심이다 — 조용히 보내면 모델이 이미지를 무시하고 글만 보고 답하는데, 검수에서 그건
"이미지를 안 보고 승인"이다.
"""

import base64

import pytest

from src.services import llm
from src.services.llm import anthropic_sdk, google_sdk, openai_sdk, registry
from src.services.llm.base import (
    ADAPTER_ANTHROPIC_SDK,
    ADAPTER_GOOGLE_SDK,
    ADAPTER_OPENAI_SDK,
    PROVIDER_ANTHROPIC,
    PROVIDER_GOOGLE,
    PROVIDER_OPENAI,
    LlmConfigError,
    LlmRequest,
    ResolvedModel,
    image_part,
    message_has_images,
    text_part,
)

PNG = b"\x89PNG\r\n\x1a\nabc"


def _image_messages() -> list:
    return [
        {"role": "system", "content": "검수자"},
        {"role": "user", "content": [text_part("게시물"), image_part(data=PNG, content_type="image/png")]},
    ]


# ── 조각 모양 ────────────────────────────────────────────────────────────────
def test_image_part_is_openai_compatible_data_url() -> None:
    part = image_part(data=PNG, content_type="image/png")

    assert part["type"] == "image_url"
    assert part["image_url"] == {
        "url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
    }


def test_text_part_shape() -> None:
    assert text_part("안녕") == {"type": "text", "text": "안녕"}


def test_message_has_images_detects_only_image_parts() -> None:
    assert message_has_images(_image_messages()) is True
    assert message_has_images([{"role": "user", "content": "글만"}]) is False
    assert message_has_images([{"role": "user", "content": [text_part("글 조각만")]}]) is False
    assert message_has_images([]) is False


# ── 등록부 ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", ["gpt-5.6-luna", "deepseek-flash"])
def test_moderation_models_declare_image_input(model: str) -> None:
    """검수 기본·대체 모델은 이미지 입력을 받는다고 문서로 확인해 적어 두었다."""
    assert registry.resolve(model).supports_image_input is True


def test_image_input_defaults_to_false_for_other_models() -> None:
    """확인하지 않은 모델은 못 받는 것으로 둔다 — 새 모델은 문서 확인 뒤에만 True."""
    others = [m for m in registry._REGISTRY if m not in {"gpt-5.6-luna", "deepseek-flash"}]
    assert others  # 등록부에 다른 모델이 있어야 이 테스트가 의미 있다
    assert all(registry.resolve(m).supports_image_input is False for m in others)


# ── 통로 검사 ────────────────────────────────────────────────────────────────
def _fake(model: str, adapter: str, provider: str, *, images: bool) -> ResolvedModel:
    return ResolvedModel(
        model=model, provider=provider, adapter=adapter, use_thinking=False,
        supports_image_input=images,
    )


async def test_complete_rejects_images_for_model_without_support(monkeypatch) -> None:
    monkeypatch.setitem(
        registry._REGISTRY, "text-only", _fake("text-only", ADAPTER_OPENAI_SDK, PROVIDER_OPENAI, images=False)
    )
    called = False

    async def fake_complete(req, resolved):
        nonlocal called
        called = True

    monkeypatch.setattr(openai_sdk, "complete", fake_complete)

    with pytest.raises(LlmConfigError, match="이미지"):
        await llm.complete(LlmRequest(model="text-only", messages=_image_messages()))
    assert called is False  # 공급자에 요청이 나가기 전에 막는다


def test_stream_rejects_images_for_model_without_support(monkeypatch) -> None:
    monkeypatch.setitem(
        registry._REGISTRY, "text-only", _fake("text-only", ADAPTER_OPENAI_SDK, PROVIDER_OPENAI, images=False)
    )

    with pytest.raises(LlmConfigError, match="이미지"):
        llm.stream(LlmRequest(model="text-only", messages=_image_messages()))


async def test_complete_passes_images_through_for_supporting_model(monkeypatch) -> None:
    monkeypatch.setitem(
        registry._REGISTRY, "vision", _fake("vision", ADAPTER_OPENAI_SDK, PROVIDER_OPENAI, images=True)
    )
    received = {}

    async def fake_complete(req, resolved):
        received["messages"] = req.messages
        return "ok"

    monkeypatch.setattr(openai_sdk, "complete", fake_complete)

    await llm.complete(LlmRequest(model="vision", messages=_image_messages()))
    assert received["messages"] == _image_messages()


async def test_text_only_request_is_unaffected(monkeypatch) -> None:
    """이미지 없는 요청은 지원 표시와 무관하게 그대로 간다 — 기존 호출부 회귀 방지."""
    monkeypatch.setitem(
        registry._REGISTRY, "text-only", _fake("text-only", ADAPTER_OPENAI_SDK, PROVIDER_OPENAI, images=False)
    )

    async def fake_complete(req, resolved):
        return "ok"

    monkeypatch.setattr(openai_sdk, "complete", fake_complete)

    assert await llm.complete(LlmRequest(model="text-only", messages=[{"role": "user", "content": "글"}])) == "ok"


# ── 어댑터 검사: 등록부가 잘못 표시해도 이미지가 조용히 사라지지 않는다 ─────────
def test_openai_adapter_sends_content_parts_as_is() -> None:
    resolved = _fake("vision", ADAPTER_OPENAI_SDK, PROVIDER_OPENAI, images=True)
    kwargs = openai_sdk._build_kwargs(LlmRequest(model="vision", messages=_image_messages()), resolved)
    assert kwargs["messages"] == _image_messages()


def test_anthropic_adapter_refuses_images_it_cannot_translate() -> None:
    resolved = _fake("claude-x", ADAPTER_ANTHROPIC_SDK, PROVIDER_ANTHROPIC, images=True)
    with pytest.raises(LlmConfigError, match="이미지"):
        anthropic_sdk._build_kwargs(LlmRequest(model="claude-x", messages=_image_messages()), resolved)


def test_google_adapter_refuses_images_it_cannot_translate() -> None:
    resolved = _fake("gemini-x", ADAPTER_GOOGLE_SDK, PROVIDER_GOOGLE, images=True)
    with pytest.raises(LlmConfigError, match="이미지"):
        google_sdk._build_config(LlmRequest(model="gemini-x", messages=_image_messages()), resolved)
