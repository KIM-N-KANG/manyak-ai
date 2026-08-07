"""LLM 등록부·기동 검사 테스트 (KNK-670).

settings는 전역을 고쳐 쓰지 않고 `Settings(_env_file=None, ...)`로 새로 만들어 모듈에 끼운다
(test_config.py와 동일 규약) — 레포의 실제 .env 값에 결과가 흔들리지 않게.
"""

from datetime import date
from decimal import Decimal

import pytest

from src.core.config import Settings
from src.services import llm
from src.services.llm import registry
from src.services.llm.base import (
    ADAPTER_ANTHROPIC_SDK,
    ADAPTER_OPENAI_SDK,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_OPENAI,
    STRUCTURED_OUTPUT_JSON_OBJECT,
    STRUCTURED_OUTPUT_JSON_SCHEMA,
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
        "OPENAI_API_KEY",
        "OPENAI_API_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: str) -> Settings:
    """레포의 .env를 무시하고 필요한 값만 채운 Settings."""
    values: dict[str, str] = {
        "deepseek_api_key": "deepseek-test-key",
        "openai_api_key": "openai-test-key",
    } | overrides
    return Settings(_env_file=None, **values)


# ── 모델 해석 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_resolve_deepseek_models(model: str) -> None:
    """실사용 DeepSeek 2종은 deepseek 공급자 + OpenAI SDK 어댑터로 해석된다."""
    resolved = registry.resolve(model)

    assert resolved.model == model
    assert resolved.provider == PROVIDER_DEEPSEEK
    assert resolved.adapter == ADAPTER_OPENAI_SDK


@pytest.mark.parametrize("model", ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4-mini"])
def test_resolve_openai_models(model: str) -> None:
    """등록한 GPT 3종은 OpenAI 공급자 + OpenAI SDK 어댑터로 해석된다."""
    resolved = registry.resolve(model)

    assert resolved.model == model
    assert resolved.provider == PROVIDER_OPENAI
    assert resolved.adapter == ADAPTER_OPENAI_SDK


def test_terra_uses_medium_reasoning_without_temperature() -> None:
    """컴파일용 Terra는 실측으로 고른 medium 추론을 쓰고 temperature는 보내지 않는다."""
    resolved = registry.resolve("gpt-5.6-terra")

    assert (resolved.use_thinking, resolved.supports_temperature) == (True, False)
    assert resolved.reasoning_effort == "medium"


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.4-mini"])
def test_other_openai_models_keep_reasoning_disabled(model: str) -> None:
    """Terra 설정을 바꿔도 다른 GPT 모델의 비추론 정책은 그대로다."""
    resolved = registry.resolve(model)

    assert (resolved.use_thinking, resolved.supports_temperature) == (False, False)


def test_resolve_claude_sonnet_5() -> None:
    """Claude Sonnet 5는 Anthropic 공급자 + Anthropic SDK 어댑터로 해석된다."""
    resolved = registry.resolve("claude-sonnet-5")

    assert resolved.provider == PROVIDER_ANTHROPIC
    assert resolved.adapter == ADAPTER_ANTHROPIC_SDK
    assert (resolved.use_thinking, resolved.supports_temperature) == (False, False)


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


@pytest.mark.parametrize(
    ("model", "input_price", "cache_read_price", "output_price"),
    [
        ("deepseek-v4-pro", "0.435", "0.003625", "0.87"),
        ("deepseek-v4-flash", "0.14", "0.0028", "0.28"),
        ("gpt-5.6-terra", "2.50", "0.25", "15.00"),
        ("gpt-5.6-luna", "1.00", "0.10", "6.00"),
        ("gpt-5.4-mini", "0.75", "0.075", "4.50"),
        ("claude-sonnet-5", "2.00", "0.20", "10.00"),
    ],
)
def test_registered_model_pricing_per_million_tokens(
    model: str,
    input_price: str,
    cache_read_price: str,
    output_price: str,
) -> None:
    """모든 실사용 모델이 2026-07-29 기준 입력·캐시 읽기·출력 USD 단가를 가진다."""
    price = registry.resolve(model).pricing_on(date(2026, 7, 29))

    assert price.input_usd_per_1m_tokens == Decimal(input_price)
    assert price.cache_read_input_usd_per_1m_tokens == Decimal(cache_read_price)
    assert price.output_usd_per_1m_tokens == Decimal(output_price)
    assert price.verified_on == date(2026, 7, 29)
    assert price.source_url.startswith("https://")


def test_every_registered_model_has_pricing() -> None:
    """앞으로 모델을 등록할 때 가격표를 빠뜨리면 이 테스트가 막는다."""
    assert all(resolved.pricing for resolved in registry._REGISTRY.values())


@pytest.mark.parametrize(
    ("model", "context_window", "max_output", "reasoning_effort", "structured_modes"),
    [
        ("deepseek-v4-pro", 1_000_000, 384_000, None, {STRUCTURED_OUTPUT_JSON_OBJECT}),
        ("deepseek-v4-flash", 1_000_000, 384_000, None, {STRUCTURED_OUTPUT_JSON_OBJECT}),
        (
            "gpt-5.6-terra",
            1_050_000,
            128_000,
            "medium",
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA},
        ),
        (
            "gpt-5.6-luna",
            1_050_000,
            128_000,
            "none",
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA},
        ),
        (
            "gpt-5.4-mini",
            400_000,
            128_000,
            "none",
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA},
        ),
        (
            "claude-sonnet-5",
            1_000_000,
            128_000,
            None,
            {STRUCTURED_OUTPUT_JSON_SCHEMA},
        ),
    ],
)
def test_registered_model_capabilities(
    model: str,
    context_window: int,
    max_output: int,
    reasoning_effort: str | None,
    structured_modes: set[str],
) -> None:
    """공식 문서에서 확인한 한도·추론·구조화 출력 능력을 모델마다 고정한다."""
    resolved = registry.resolve(model)

    assert resolved.context_window_tokens == context_window
    assert resolved.max_output_tokens == max_output
    assert resolved.reasoning_effort == reasoning_effort
    assert resolved.structured_output_modes == frozenset(structured_modes)
    assert resolved.capabilities_verified_on == date(2026, 7, 29)
    assert resolved.capabilities_source_urls
    assert all(url.startswith("https://") for url in resolved.capabilities_source_urls)


def test_every_registered_model_has_complete_valid_capabilities() -> None:
    """앞으로 모델 등록 시 한도·추론 목록·구조화 출력·근거 누락이나 모순을 막는다."""
    for resolved in registry._REGISTRY.values():
        assert resolved.context_window_tokens is not None
        assert resolved.max_output_tokens is not None
        assert 0 < resolved.max_output_tokens <= resolved.context_window_tokens
        assert resolved.supported_reasoning_efforts
        if resolved.reasoning_effort is not None:
            assert resolved.reasoning_effort in resolved.supported_reasoning_efforts
        assert resolved.structured_output_modes
        assert resolved.capabilities_verified_on is not None
        assert resolved.capabilities_source_urls


def test_available_pinned_snapshots_are_recorded() -> None:
    """공식적으로 확인되는 고정 스냅샷만 적고, 없는 ID를 지어내지 않는다."""
    assert registry.resolve("gpt-5.4-mini").snapshot_model == "gpt-5.4-mini-2026-03-17"
    assert registry.resolve("claude-sonnet-5").snapshot_model == "claude-sonnet-5"
    assert registry.resolve("gpt-5.6-terra").snapshot_model is None
    assert registry.resolve("gpt-5.6-luna").snapshot_model is None
    assert registry.resolve("deepseek-v4-pro").snapshot_model is None
    assert registry.resolve("deepseek-v4-flash").snapshot_model is None


def test_claude_sonnet_5_pricing_switches_after_introductory_period() -> None:
    """Sonnet 5의 공식 할인 종료일 다음 날부터 예정된 표준 단가를 고른다."""
    model = registry.resolve("claude-sonnet-5")

    introductory = model.pricing_on(date(2026, 8, 31))
    standard = model.pricing_on(date(2026, 9, 1))

    assert (
        introductory.input_usd_per_1m_tokens,
        introductory.cache_write_input_usd_per_1m_tokens,
        introductory.cache_read_input_usd_per_1m_tokens,
        introductory.output_usd_per_1m_tokens,
    ) == (Decimal("2.00"), Decimal("2.50"), Decimal("0.20"), Decimal("10.00"))
    assert (
        standard.input_usd_per_1m_tokens,
        standard.cache_write_input_usd_per_1m_tokens,
        standard.cache_read_input_usd_per_1m_tokens,
        standard.output_usd_per_1m_tokens,
    ) == (Decimal("3.00"), Decimal("3.75"), Decimal("0.30"), Decimal("15.00"))


def test_gpt_5_6_pricing_includes_cache_write_and_long_context_rules() -> None:
    """현재 Terra 표준 단가와 별도 캐시 쓰기·272K 초과 할증을 보존한다."""
    previous = registry.resolve("gpt-5.6-terra").pricing_on(date(2026, 7, 29))
    terra = registry.resolve("gpt-5.6-terra").pricing_on(date(2026, 8, 7))

    assert (
        previous.input_usd_per_1m_tokens,
        previous.cache_read_input_usd_per_1m_tokens,
        previous.cache_write_input_usd_per_1m_tokens,
        previous.output_usd_per_1m_tokens,
    ) == (Decimal("2.50"), Decimal("0.25"), Decimal("3.125"), Decimal("15.00"))
    assert previous.effective_until == date(2026, 7, 29)
    assert (
        terra.input_usd_per_1m_tokens,
        terra.cache_read_input_usd_per_1m_tokens,
        terra.cache_write_input_usd_per_1m_tokens,
        terra.output_usd_per_1m_tokens,
    ) == (Decimal("2.00"), Decimal("0.20"), Decimal("2.50"), Decimal("12.00"))
    assert terra.verified_on == date(2026, 8, 7)
    assert terra.effective_from == date(2026, 7, 30)
    assert terra.source_url == "https://developers.openai.com/api/docs/pricing"
    assert terra.long_context_threshold_tokens == 272_000
    assert terra.long_context_input_multiplier == Decimal("2")
    assert terra.long_context_output_multiplier == Decimal("1.5")


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


def test_credentials_for_openai(monkeypatch) -> None:
    """OpenAI 접속 정보와 고칠 env 이름을 함께 읽는다."""
    monkeypatch.setattr(registry, "settings", _settings(openai_api_key="openai-key"))

    creds = registry.credentials(PROVIDER_OPENAI)

    assert creds.api_key == "openai-key"
    assert creds.base_url is None
    assert (creds.api_key_env, creds.base_url_env) == ("OPENAI_API_KEY", "OPENAI_API_URL")


def test_credentials_unknown_provider_rejected() -> None:
    """접속 규칙이 없는 공급자는 조용히 넘어가지 않는다."""
    with pytest.raises(LlmConfigError):
        registry.credentials("nonexistent-provider")


# ── 기동 검사 ────────────────────────────────────────────────────────────────
def test_startup_checks_the_key_only_of_selected_models(monkeypatch) -> None:
    """기동 검사는 **선택된** 모델의 공급자 키만 본다 — 안 쓰는 공급자의 키가 비어도 뜬다(KNK-675).

    이 성질이 Anthropic 키를 필수로 만들지 않은 근거다. 깨지면 이 공급자를 안 쓰는 환경이
    통째로 안 뜬다 — CI는 현재 선택 가능한 DeepSeek·OpenAI의 더미 키를 주입해 컨테이너를
    실제로 띄워 스모크 검사한다(`docker-image.yml`).

    **"빈 키로 통과한다"만 보면 아무것도 증명되지 않는다.** 선택된 모델이 그 공급자를 안 쓰니
    당연히 통과하고, 키를 필수로 되돌려도 이 테스트는 그대로 통과한다(실제로 그랬다). 그래서
    같은 빈 키가 **고르는 순간 막는지**를 짝으로 확인한다 — 통과와 거부를 가르는 것이 정말
    "선택 여부"임을 이 대비가 고정한다.
    """
    # 안 고르면 — Anthropic 키가 비어도 기동한다
    monkeypatch.setattr(registry, "settings", _settings(anthropic_api_key=""))
    registry.validate_selected_models()

    # 고르면 — 같은 빈 키가 이제는 기동을 막고, 어느 env에 무엇을 채울지 알려준다
    monkeypatch.setattr(
        registry,
        "settings",
        _settings(anthropic_api_key="", storylines_model="claude-sonnet-5"),
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
        _settings(anthropic_api_key="ant-key", storylines_model="claude-sonnet-5"),
    )
    registry.validate_selected_models()


def test_startup_requires_openai_key_only_when_openai_model_is_selected(monkeypatch) -> None:
    """OpenAI 키도 GPT를 고른 순간에만 필수가 된다."""
    monkeypatch.setattr(
        registry,
        "settings",
        _settings(openai_api_key="", story_compile_model="deepseek-v4-pro"),
    )
    registry.validate_selected_models()

    monkeypatch.setattr(
        registry,
        "settings",
        _settings(
            openai_api_key="",
            story_compile_model="deepseek-v4-pro",
            storylines_model="gpt-5.6-terra",
        ),
    )
    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()
    message = str(exc_info.value)
    assert "STORYLINES_MODEL" in message
    assert "OPENAI_API_KEY" in message

    monkeypatch.setattr(
        registry,
        "settings",
        _settings(
            openai_api_key="openai-key",
            story_compile_model="deepseek-v4-pro",
            storylines_model="gpt-5.6-terra",
        ),
    )
    registry.validate_selected_models()


def test_selected_models_covers_three_env_vars(monkeypatch) -> None:
    """용도별 모델 3개를 env 이름과 함께 돌려준다(KNK-595 3분리와 짝)."""
    monkeypatch.setattr(registry, "settings", _settings())

    assert registry.selected_models() == (
        ("STORYLINES_MODEL", "deepseek-v4-flash"),
        ("STORY_COMPILE_MODEL", "gpt-5.6-terra"),
        ("CHAT_MODEL", "deepseek-v4-flash"),
    )


def test_validate_passes_with_default_models(monkeypatch) -> None:
    """코드 기본 모델들이 요구하는 공급자 키가 있으면 기동 검사를 통과한다."""
    monkeypatch.setattr(registry, "settings", _settings())

    registry.validate_selected_models()  # 예외 없이 통과


def test_validate_default_models_requires_openai_key(monkeypatch) -> None:
    """기본 컴파일 모델 Terra를 쓸 때 OpenAI 키가 비면 어느 설정이 문제인지 알린다."""
    monkeypatch.setattr(registry, "settings", _settings(openai_api_key=""))

    with pytest.raises(LlmConfigError) as exc_info:
        registry.validate_selected_models()

    message = str(exc_info.value)
    assert "STORY_COMPILE_MODEL" in message
    assert "OPENAI_API_KEY" in message


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

    DeepSeek은 주소에 기본값이 있어 None이 되지 않는다. 주소 없이 부르는 OpenAI·Anthropic이
    이 갈래를 탄다.
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
