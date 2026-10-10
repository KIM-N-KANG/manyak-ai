"""KNK-1448: 모의 HTTP로 선택형 판정의 전송·검증·실패·취소를 검증한다."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import Mock, call

import httpx
import pytest

from src.core.config import Settings
from src.services import llm
from src.services.llm import registry, typesafe_api
from src.services.llm.base import (
    ChoiceQuestion, EvaluationRequest, LlmBadRequest, LlmConfigError, LlmInvalidResponse,
    LlmRateLimited, LlmRequest, LlmTimeout, LlmUnavailable,
)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # 실제 로컬 키나 환경변수를 쓰지 않는다.
    for key in ("JEV_MODEL", "TYPESAFE_API_KEY", "TYPESAFE_API_URL"):
        monkeypatch.delenv(key, raising=False)
    value = Settings(_env_file=None, deepseek_api_key="test", openai_api_key="test",
                     typesafe_api_key="test-typesafe-key")
    monkeypatch.setattr(registry, "settings", value)
    return value


@pytest.fixture
def req():
    return EvaluationRequest(
        model="jev-1.13.0", timeout=1,
        state={"target_character": "test", "current_turn": {"assistant": "private-input"}},
        questions={
            "emotion": ChoiceQuestion("Choose the emotion", {"fear": "afraid", "joy": "happy"}),
            "fear_intensity": ChoiceQuestion("Rate fear", {"low": "mild", "high": "strong"}),
        },
    )


def valid_response() -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "emotion": {"type": "choice", "choice": "fear", "confidence": .78,
                        "probabilities": {"fear": .8, "joy": .2}},
            "fear_intensity": {"type": "choice", "choice": "high", "confidence": .47,
                               "probabilities": {"low": .41, "high": .58}},
        },
        "usage": {"input_tokens": 321, "output_tokens": 42},
    }


def install_http(monkeypatch, handler):
    client_type = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        client = client_type(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(typesafe_api.httpx, "AsyncClient", factory)
    return clients


async def test_public_evaluate_batches_and_preserves_typed_answers(monkeypatch, req):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-typesafe-key"
        payload = json.loads(request.content)
        assert payload["state"] == req.state
        assert set(payload["questions"]) == {"emotion", "fear_intensity"}
        assert payload["questions"]["fear_intensity"]["type"] == "choice"
        assert "messages" not in payload and "temperature" not in payload
        return httpx.Response(200, json=valid_response())

    clients = install_http(monkeypatch, handler)
    generation = Mock()
    metadata = {}

    @contextmanager
    def observe(name, **kwargs):
        metadata.update(kwargs)
        yield generation

    monkeypatch.setattr(typesafe_api.langfuse, "observe_generation", observe)
    result = await llm.evaluate(req)
    assert len(requests) == 1 and all(client.is_closed for client in clients)
    assert result.model == req.model and result.provider == "typesafe"
    assert result.answers["emotion"].choice == "fear"
    assert result.answers["emotion"].probabilities["fear"] == .8
    assert result.answers["fear_intensity"].choice == "high"
    assert result.answers["fear_intensity"].confidence == .47
    assert result.usage.input_tokens == 321 and result.usage.output_tokens == 42
    assert "input_data" not in metadata
    assert generation.finish.call_args_list == [
        call(usage_details={"input": 321, "output": 42}),
        call(output={"answer_count": 2}),
    ]
    assert "private-input" not in repr(req)


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 321, "output_tokens": 42}, {"input": 321, "output": 42}),
    ({"input_tokens": 321}, {"input": 321}),
    ({"input_tokens": 0, "output_tokens": 0}, {"input": 0, "output": 0}),
    ({"input_tokens": True, "output_tokens": 42}, {"output": 42}),
    ({"input_tokens": -1, "output_tokens": "42"}, {}),
    ({"input_tokens": None, "output_tokens": 1.5}, {}),
    ({}, {}),
    (None, {}),
])
async def test_invalid_answers_preserve_only_valid_usage(monkeypatch, req, usage, expected):
    data = valid_response()
    data["answers"] = {}
    data["usage"] = usage
    install_http(monkeypatch, lambda request: httpx.Response(200, json=data))
    generation = Mock()

    @contextmanager
    def observe(*args, **kwargs):
        yield generation

    monkeypatch.setattr(typesafe_api.langfuse, "observe_generation", observe)
    with pytest.raises(LlmInvalidResponse):
        await llm.evaluate(req)
    assert generation.finish.call_args_list == (
        [call(usage_details=expected)] if expected else []
    )


@pytest.mark.parametrize("status,exception", [
    (400, LlmBadRequest), (422, LlmUnavailable), (429, LlmRateLimited),
    (401, LlmUnavailable), (403, LlmUnavailable), (500, LlmUnavailable),
    (529, LlmUnavailable), (302, LlmUnavailable),
])
async def test_http_failures_never_retry_or_expose_body(monkeypatch, req, status, exception):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="private-input test-typesafe-key",
                              headers={"location": "https://other.example/", "retry-after": "1"})

    clients = install_http(monkeypatch, handler)
    with pytest.raises(exception) as caught:
        await llm.evaluate(req)
    assert caught.value.provider == "typesafe" and caught.value.model == req.model
    assert "private-input" not in str(caught.value) and "test-typesafe-key" not in str(caught.value)
    assert len(requests) == 1 and all(client.is_closed for client in clients)


@pytest.mark.parametrize("error,expected", [
    (httpx.ReadTimeout("private-input"), LlmTimeout),
    (httpx.ConnectError("private-input"), LlmUnavailable),
])
async def test_network_errors(monkeypatch, req, error, expected):
    calls = []

    def handler(request):
        calls.append(request)
        raise error

    clients = install_http(monkeypatch, handler)
    with pytest.raises(expected, match="TypeSafe") as caught:
        await llm.evaluate(req)
    assert "private-input" not in str(caught.value)
    assert len(calls) == 1 and all(client.is_closed for client in clients)


@pytest.mark.parametrize("cancel", [False, True])
async def test_total_timeout_and_external_cancellation_close_client(monkeypatch, req, cancel):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    clients = install_http(monkeypatch, handler)
    task = asyncio.create_task(llm.evaluate(replace(req, timeout=1 if cancel else .03)))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else LlmTimeout):
        await task
    assert stopped.is_set() and all(client.is_closed for client in clients)


@pytest.mark.parametrize("case", [
    "model", "missing", "extra", "type", "choice", "not_max", "confidence", "nan", "bool",
    "negative", "huge", "sum", "missing_option", "usage", "usage_bool", "json", "size",
])
async def test_malformed_response_rejected(monkeypatch, req, case):
    data = valid_response()
    answer = data["answers"]["emotion"]
    if case == "model": data["model"] = "jev-latest"
    elif case == "missing": del data["answers"]["fear_intensity"]
    elif case == "extra": data["answers"]["extra"] = answer
    elif case == "type": answer["type"] = "score"
    elif case == "choice": answer["choice"] = "not-an-option"
    elif case == "not_max": answer["choice"] = "joy"
    elif case == "confidence": answer["confidence"] = 1.1
    elif case == "nan": answer["probabilities"]["fear"] = float("nan")
    elif case == "bool": answer["probabilities"]["fear"] = True
    elif case == "negative": answer["probabilities"]["joy"] = -.2
    elif case == "huge": answer["probabilities"]["fear"] = 10 ** 1000
    elif case == "sum": answer["probabilities"]["joy"] = .5
    elif case == "missing_option": del answer["probabilities"]["joy"]
    elif case == "usage": data["usage"] = None
    elif case == "usage_bool": data["usage"]["input_tokens"] = True
    content = json.dumps(data).encode()
    if case == "json": content = b"private-input not json"
    if case == "size": monkeypatch.setattr(typesafe_api, "_MAX_RESPONSE_BYTES", 4)
    install_http(monkeypatch, lambda request: httpx.Response(200, content=content))
    with pytest.raises(LlmInvalidResponse) as caught:
        await llm.evaluate(req)
    assert "private-input" not in str(caught.value)


async def test_missing_usage_is_not_reported_as_zero(monkeypatch, req):
    data = valid_response()
    data["usage"] = {}
    install_http(monkeypatch, lambda request: httpx.Response(200, json=data))
    result = await llm.evaluate(req)
    assert result.usage.input_tokens is None and result.usage.output_tokens is None


@pytest.mark.parametrize("key", ["", " ", "line\nbreak", "한글", "test\x00key"])
async def test_bad_key_fails_before_network(monkeypatch, settings, req, key):
    settings.typesafe_api_key = key
    install_http(monkeypatch, lambda request: pytest.fail("network must not be reached"))
    with pytest.raises(LlmConfigError, match="TYPESAFE_API_KEY"):
        await llm.evaluate(req)


@pytest.mark.parametrize("change", [
    {"model": "unregistered"}, {"model": "deepseek-flash"}, {"timeout": 0},
    {"timeout": float("nan")}, {"timeout": True}, {"questions": {}}, {"state": None},
    {"state": {"invalid": object()}},
    {"questions": {"x": ChoiceQuestion("", {"yes": "yes", "no": "no"})}},
])
async def test_invalid_request_does_not_call_provider(monkeypatch, req, change):
    install_http(monkeypatch, lambda request: pytest.fail("network must not be reached"))
    with pytest.raises(LlmConfigError):
        await llm.evaluate(replace(req, **change))


def test_registry_metadata_and_optional_key_at_startup(settings):
    settings.typesafe_api_key = ""
    llm.validate_startup()
    model = registry.resolve(settings.jev_model)
    assert llm.provider_of(settings.jev_model) == "typesafe"
    assert model.pricing_on().input_usd_per_1m_tokens == Decimal("0.042")
    assert model.pricing_on().output_usd_per_1m_tokens == 0


@pytest.mark.parametrize("field,value", [
    ("chat_model", "jev-1.13.0"), ("story_compile_model", "jev-1.13.0"),
    ("jev_model", "deepseek-flash"), ("jev_model", "unregistered"),
    ("typesafe_api_url", "http://api.typesafe.ai"),
    ("typesafe_api_url", "https://user:password@api.typesafe.ai"),
    ("typesafe_api_url", "https://api.typesafe.ai/path"),
])
def test_startup_rejects_wrong_model_or_url(settings, field, value):
    setattr(settings, field, value)
    with pytest.raises(LlmConfigError):
        llm.validate_startup()


async def test_text_calls_cannot_use_evaluation_model():
    req = LlmRequest(model="jev-1.13.0", messages=[])
    with pytest.raises(LlmConfigError):
        await llm.complete(req)
    with pytest.raises(LlmConfigError):
        llm.stream(req)
