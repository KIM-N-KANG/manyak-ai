"""LLM 등록부·기동 검사 테스트 (KNK-670).

settings는 전역을 고쳐 쓰지 않고 `Settings(_env_file=None, ...)`로 새로 만들어 모듈에 끼운다
(test_config.py와 동일 규약) — 레포의 실제 .env 값에 결과가 흔들리지 않게.
"""

import pytest

from src.core.config import Settings
from src.services.llm import registry
from src.services.llm.base import (
    ADAPTER_OPENAI_SDK,
    PROVIDER_DEEPSEEK,
    LlmConfigError,
    LlmError,
    LlmTimeout,
    ResolvedModel,
)


@pytest.fixture(autouse=True)
def _clear_model_env(monkeypatch) -> None:
    """셸·컨테이너에 켜둔 모델 env를 지운다.

    `Settings(_env_file=None)`은 .env 파일만 무시하고 os.environ은 계속 읽는다. 누가
    `export CHAT_MODEL=...`을 켜둔 채 돌리면 "코드 기본값" 단언이 그 사람 컴퓨터에서만 깨지고,
    도커·CI에서는 재현되지 않아 원인을 찾기 어렵다.
    """
    for name in ("STORYLINES_MODEL", "STORY_COMPILE_MODEL", "CHAT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: str) -> Settings:
    """레포의 .env를 무시하고 필요한 값만 채운 Settings."""
    values: dict[str, str] = {"deepseek_api_key": "test-key"} | overrides
    return Settings(_env_file=None, **values)


# ── 모델 해석 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_resolve_deepseek_models(model: str) -> None:
    """실사용 DeepSeek 2종은 deepseek 공급자 + OpenAI SDK 어댑터로 해석된다."""
    resolved = registry.resolve(model)

    assert resolved.model == model
    assert resolved.provider == PROVIDER_DEEPSEEK
    assert resolved.adapter == ADAPTER_OPENAI_SDK


def test_registry_holds_meaning_not_provider_syntax() -> None:
    """호출 특성을 **뜻**으로 담는다 — 회사 문법(extra_body 등)은 등록부에 없고 어댑터가 만든다."""
    resolved = registry.resolve("deepseek-v4-flash")

    assert resolved.use_thinking is False  # DeepSeek은 비추론으로 부른다(KNK-208 벤치)
    assert resolved.supports_temperature is True
    assert not hasattr(resolved, "extra_body")  # 공급자 문법 덩어리는 들지 않는다


def test_each_model_declares_its_own_settings() -> None:
    """모델마다 자기 설정을 따로 적는다 — 한 모델을 고칠 때 다른 모델이 딸려 바뀌지 않는다.

    두 모델을 각각 단언한다. 나중에 한쪽만 조정해도 다른 쪽 값이 이 테스트로 고정된다.
    """
    pro = registry.resolve("deepseek-v4-pro")
    flash = registry.resolve("deepseek-v4-flash")

    assert (pro.use_thinking, pro.supports_temperature) == (False, True)
    assert (flash.use_thinking, flash.supports_temperature) == (False, True)


def test_resolve_unknown_model_lists_known_models() -> None:
    """미등록 모델은 거부하고, 무엇이 등록돼 있는지 메시지로 알려준다."""
    with pytest.raises(LlmConfigError) as exc_info:
        registry.resolve("deepseek-v9-imaginary")

    message = str(exc_info.value)
    assert "deepseek-v9-imaginary" in message
    assert "deepseek-v4-flash" in message


# ── 공급자 접속 정보 ──────────────────────────────────────────────────────────
def test_credentials_read_settings_at_call_time(monkeypatch) -> None:
    """키·주소는 import 시점이 아니라 호출 시점의 설정에서 읽는다."""
    monkeypatch.setattr(
        registry,
        "settings",
        _settings(deepseek_api_key="late-key", deepseek_api_url="https://example.test"),
    )

    creds = registry.credentials(PROVIDER_DEEPSEEK)

    assert creds.api_key == "late-key"
    assert creds.base_url == "https://example.test"
    assert creds.api_key_env == "DEEPSEEK_API_KEY"


def test_credentials_unknown_provider_rejected() -> None:
    """접속 규칙이 없는 공급자는 조용히 넘어가지 않는다."""
    with pytest.raises(LlmConfigError):
        registry.credentials("nonexistent-provider")


# ── 기동 검사 ────────────────────────────────────────────────────────────────
def test_selected_models_covers_three_env_vars(monkeypatch) -> None:
    """용도별 모델 3개를 env 이름과 함께 돌려준다(KNK-595 3분리와 짝)."""
    monkeypatch.setattr(registry, "settings", _settings())

    assert registry.selected_models() == (
        ("STORYLINES_MODEL", "deepseek-v4-flash"),
        ("STORY_COMPILE_MODEL", "deepseek-v4-pro"),
        ("CHAT_MODEL", "deepseek-v4-flash"),
    )


def test_validate_passes_with_default_models(monkeypatch) -> None:
    """코드 기본값(DeepSeek 2종) + 키가 있으면 통과한다 — CI·팀 로컬이 그대로 뜬다."""
    monkeypatch.setattr(registry, "settings", _settings())

    registry.validate_selected_models()  # 예외 없이 통과


def test_validate_rejects_unregistered_selected_model(monkeypatch) -> None:
    """선택된 모델이 미등록이면 기동에서 실패하고, 어느 env가 문제인지 가리킨다."""
    monkeypatch.setattr(registry, "settings", _settings(storylines_model="gpt-9-unlisted"))

    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()

    message = str(exc_info.value)
    assert "STORYLINES_MODEL" in message
    assert "gpt-9-unlisted" in message


def test_validate_rejects_blank_provider_key(monkeypatch) -> None:
    """선택된 모델의 공급자 키가 비면(공백 포함) 기동에서 실패하고 채울 env를 알려준다."""
    monkeypatch.setattr(registry, "settings", _settings(deepseek_api_key="   "))

    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()

    assert "DEEPSEEK_API_KEY" in str(exc_info.value)


def test_validate_names_the_env_for_provider_without_credentials(monkeypatch) -> None:
    """접속 정보 규칙이 없는 공급자에서 막힐 때도 어느 env가 문제인지 붙인다."""
    future = ResolvedModel(
        model="claude-sonnet-5",
        provider="anthropic",  # 아직 이 공급자의 키·주소 규칙이 없다(KNK-675)
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=False,
    )
    monkeypatch.setitem(registry._REGISTRY, "claude-sonnet-5", future)
    monkeypatch.setattr(registry, "settings", _settings(chat_model="claude-sonnet-5"))

    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()

    assert "CHAT_MODEL" in str(exc_info.value)


@pytest.mark.parametrize("bad_url", ["not-a-url", "ftp://api.deepseek.com", "https://", "   "])
def test_validate_rejects_malformed_base_url(monkeypatch, bad_url: str) -> None:
    """주소가 틀리면 기동에서 실패한다 — 비어 있지 않아도 틀릴 수 있어 첫 호출까지 숨는다."""
    monkeypatch.setattr(registry, "settings", _settings(deepseek_api_url=bad_url))

    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()

    assert "DEEPSEEK_API_URL" in str(exc_info.value)


def test_validate_allows_missing_base_url() -> None:
    """주소가 아예 없으면 통과한다 — SDK 기본 주소를 쓰겠다는 뜻이라 오류가 아니다.

    DeepSeek은 주소에 기본값이 있어 None이 되지 않는다. 주소 없이 부르는 공급자(다음 단계에
    등록될 GPT·Anthropic)가 이 갈래를 탄다.
    """
    creds = registry.ProviderCredentials(
        api_key="k", base_url=None, api_key_env="X_API_KEY", base_url_env="X_API_URL"
    )

    registry._validate_base_url(creds, env_name="CHAT_MODEL", model="m", provider="x")


def test_config_error_is_not_a_provider_error() -> None:
    """설정 오류는 전송 오류 계열이 아니다 — 호출부의 except LlmError가 삼켜 502로 위장하면 안 된다."""
    assert not issubclass(LlmConfigError, LlmError)


def test_provider_error_carries_provider_and_model() -> None:
    """전송 오류는 provider·model을 싣고 다닌다 — 실패 경로엔 결과 객체가 없어 예외가 유일한 출처다."""
    exc = LlmTimeout("응답 시간 초과", provider=PROVIDER_DEEPSEEK, model="deepseek-v4-flash")

    assert exc.provider == PROVIDER_DEEPSEEK
    assert exc.model == "deepseek-v4-flash"
    assert isinstance(exc, LlmError)
