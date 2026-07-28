"""LLM 등록부·기동 검사 테스트 (KNK-670).

settings는 전역을 고쳐 쓰지 않고 `Settings(_env_file=None, ...)`로 새로 만들어 모듈에 끼운다
(test_config.py와 동일 규약) — 레포의 실제 .env 값에 결과가 흔들리지 않게.
"""

import pytest

from src.core.config import Settings
from src.services import llm
from src.services.llm import registry
from src.services.llm.base import (
    ADAPTER_ANTHROPIC_SDK,
    ADAPTER_OPENAI_SDK,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    LlmConfigError,
    LlmError,
    LlmTimeout,
    ResolvedModel,
)


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch) -> None:
    """셸·컨테이너에 켜둔 LLM 관련 env를 지운다 — 모델 이름과 접속 정보 둘 다.

    `Settings(_env_file=None)`은 .env 파일만 무시하고 os.environ은 계속 읽는다. 누가
    `export CHAT_MODEL=...`을 켜둔 채 돌리면 "코드 기본값" 단언이 그 사람 컴퓨터에서만 깨지고,
    도커·CI에서는 재현되지 않아 원인을 찾기 어렵다. 접속 정보도 같은 이유로 함께 지운다.
    """
    for name in (
        "STORYLINES_MODEL",
        "STORY_COMPILE_MODEL",
        "CHAT_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_URL",
    ):
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


def test_credentials_for_anthropic(monkeypatch) -> None:
    """Anthropic도 같은 규칙으로 읽는다 — 값과 함께 **고칠 env 이름**을 들고 온다(KNK-675).

    env 이름을 함께 나르는 것이 이 자료형의 존재 이유다. 키가 비었을 때 기동 검사가
    "ANTHROPIC_API_KEY를 채워라"까지 말할 수 있어야 한다.
    """
    monkeypatch.setattr(registry, "settings", _settings(anthropic_api_key="ant-key"))

    creds = registry.credentials(PROVIDER_ANTHROPIC)

    assert creds.api_key == "ant-key"
    # 주소를 안 적으면 None이다 — 빈 문자열이 아니라 "SDK 기본 주소를 쓴다"는 뜻이고,
    # 빈 문자열이면 기동 검사의 주소 형식 검사에 걸린다.
    assert creds.base_url is None
    assert (creds.api_key_env, creds.base_url_env) == ("ANTHROPIC_API_KEY", "ANTHROPIC_API_URL")


def test_credentials_unknown_provider_rejected() -> None:
    """접속 규칙이 없는 공급자는 조용히 넘어가지 않는다."""
    with pytest.raises(LlmConfigError):
        registry.credentials("nonexistent-provider")


# ── 기동 검사 ────────────────────────────────────────────────────────────────
def test_startup_checks_the_key_only_of_selected_models(monkeypatch) -> None:
    """기동 검사는 **선택된** 모델의 공급자 키만 본다 — 안 쓰는 공급자의 키가 비어도 뜬다(KNK-675).

    이 성질이 Anthropic 키를 필수로 만들지 않은 근거다. 깨지면 이 공급자를 안 쓰는 환경이
    통째로 안 뜬다 — CI는 `DEEPSEEK_API_KEY` 하나만 주입해 컨테이너를 실제로 띄워 스모크
    검사를 한다(`docker-image.yml`).

    **"빈 키로 통과한다"만 보면 아무것도 증명되지 않는다.** 선택된 모델이 그 공급자를 안 쓰니
    당연히 통과하고, 키를 필수로 되돌려도 이 테스트는 그대로 통과한다(실제로 그랬다). 그래서
    같은 빈 키가 **고르는 순간 막는지**를 짝으로 확인한다 — 통과와 거부를 가르는 것이 정말
    "선택 여부"임을 이 대비가 고정한다.
    """
    monkeypatch.setitem(
        registry._REGISTRY,
        "model-on-the-anthropic-adapter",
        ResolvedModel(
            model="model-on-the-anthropic-adapter",
            provider=PROVIDER_ANTHROPIC,
            adapter=ADAPTER_ANTHROPIC_SDK,
            use_thinking=True,
        ),
    )

    # 안 고르면 — Anthropic 키가 비어도 기동한다
    monkeypatch.setattr(registry, "settings", _settings(anthropic_api_key=""))
    registry.validate_selected_models()

    # 고르면 — 같은 빈 키가 이제는 기동을 막고, 어느 env에 무엇을 채울지 알려준다
    monkeypatch.setattr(
        registry,
        "settings",
        _settings(anthropic_api_key="", storylines_model="model-on-the-anthropic-adapter"),
    )
    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()
    message = str(exc_info.value)
    assert "STORYLINES_MODEL" in message
    assert "ANTHROPIC_API_KEY" in message

    # 골랐고 키도 있으면 — 다시 통과한다. 막은 것이 "선택"이 아니라 "빈 키"였음을 고정한다.
    monkeypatch.setattr(
        registry,
        "settings",
        _settings(anthropic_api_key="ant-key", storylines_model="model-on-the-anthropic-adapter"),
    )
    registry.validate_selected_models()


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
    """접속 정보 규칙이 없는 공급자에서 막힐 때도 어느 env가 문제인지 붙인다.

    **공급자 이름은 규칙이 생길 일이 없는 값을 쓴다.** 예전에는 여기에 "anthropic"을 썼는데,
    KNK-675에서 그 공급자의 규칙이 실제로 생기자 검사 지점이 조용히 "키가 비었다"로 옮겨갔다.
    두 오류 메시지에 모두 CHAT_MODEL이 들어 있어 테스트는 계속 통과했고, 정작 이 테스트가
    지키려던 경로는 아무도 밟지 않게 됐다.

    같은 일이 또 생기지 않도록 **어느 단계에서 막혔는지도 함께 단언한다** — env 이름만 보면
    검사 지점이 옮겨가도 알 수 없다.
    """
    future = ResolvedModel(
        model="model-of-an-unsupported-provider",
        provider="unsupported-provider",  # credentials()에 이 이름의 분기는 없다
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=True,
    )
    monkeypatch.setitem(registry._REGISTRY, "model-of-an-unsupported-provider", future)
    monkeypatch.setattr(
        registry, "settings", _settings(chat_model="model-of-an-unsupported-provider")
    )

    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()

    message = str(exc_info.value)
    assert "CHAT_MODEL" in message
    assert "접속 정보 규칙이 없습니다" in message


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


# ── provider 해석 (KNK-674) ──────────────────────────────────────────────────
# provider는 예전에 전역 설정값(LLM_PROVIDER) 하나였다. 그 값은 "지금 이 호출이 어디로
# 갔는지"와 무관해서, 스토리와 채팅을 서로 다른 회사로 나눠 쓰는 순간 절반이 거짓이 된다.
def test_provider_of_follows_the_model_not_a_fixed_value(other_provider_model) -> None:
    """모델 이름이 공급자를 정한다 — 값이 고정돼 있으면 아래 두 단언이 함께 통과할 수 없다."""
    other_provider_model()

    assert llm.provider_of("not-deepseek-model") == "not-deepseek"
    assert llm.provider_of("deepseek-v4-flash") == PROVIDER_DEEPSEEK


def test_provider_of_rejects_unregistered_model() -> None:
    """등록 안 된 모델은 어느 공급자인지 알 수 없다 — 지어내지 않고 설정 오류로 막는다."""
    with pytest.raises(LlmConfigError):
        llm.provider_of("있지도-않은-모델")
