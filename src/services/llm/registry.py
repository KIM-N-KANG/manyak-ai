"""모델 등록부 — 모델 이름을 공급자·어댑터·인자로 해석한다(KNK-670).

**모델 단위로 적는다.** 같은 회사라도 모델마다 받는 인자가 다르기 때문이다 — 예를 들어
Anthropic Sonnet 4.6은 temperature를 받지만 Sonnet 5는 400으로 거부한다. 회사 단위로 묶으면
그 차이를 담을 자리가 없다.

미등록 모델은 어느 회사로 보낼지 결정할 수 없다. 그래서 선택된 모델이 등록부에 없거나 그
모델의 공급자 키가 비어 있으면 **서버 기동에서 실패**시킨다(`validate_selected_models`).
선택되지 않은 공급자의 키 부재는 허용한다 — DeepSeek만 쓰는 환경이 다른 회사 키 없이 떠야 한다.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from src.core.config import settings
from src.services.llm.base import (
    ADAPTER_ANTHROPIC_SDK,
    ADAPTER_GOOGLE_SDK,
    ADAPTER_OPENAI_SDK,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_GOOGLE,
    PROVIDER_OPENAI,
    STRUCTURED_OUTPUT_JSON_OBJECT,
    STRUCTURED_OUTPUT_JSON_SCHEMA,
    LlmConfigError,
    ModelPricing,
    ResolvedModel,
)

# 쓸 수 있는 모델의 전부. 여기 없는 이름은 호출 대상이 될 수 없다.
#
# **모델별 설정은 모델마다 따로 적는다 — 값이 같아도 한 자리에 모으지 않는다**(사용자 결정).
# 모아두면 한 모델의 설정을 고칠 때 다른 모델이 같이 바뀐다. 그리고 여기 적는 것은 **뜻**뿐이고
# 회사별 문법은 어댑터가 만든다 — 공급자가 늘어도 이 표를 고치지 않는다.
_REGISTRY: dict[str, ResolvedModel] = {
    # 스토리 컴파일 전용(STORY_COMPILE_MODEL). 비추론 호출 — 창작 태스크에서 추론 모드가
    # 출력 외국어 오염·평면화를 일으켜 비추론이 더 안정적이었다(KNK-208 벤치).
    "deepseek-v4-pro": ResolvedModel(
        model="deepseek-v4-pro",
        provider=PROVIDER_DEEPSEEK,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=True,
        context_window_tokens=1_000_000,
        max_output_tokens=384_000,
        reasoning_effort=None,
        supported_reasoning_efforts=frozenset({"high", "max"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        capabilities_verified_on=date(2026, 7, 29),
        capabilities_source_urls=(
            "https://api-docs.deepseek.com/quick_start/pricing",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.435"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.003625"),
                output_usd_per_1m_tokens=Decimal("0.87"),
                source_url="https://api-docs.deepseek.com/quick_start/pricing",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 4, 24),
            ),
        ),
    ),
    # 스토리라인·채팅(STORYLINES_MODEL·CHAT_MODEL). 같은 비추론 정책이지만 따로 적는다 —
    # 첫 토큰 지연이 작아야 하는 경로라 나중에 이 모델만 조정할 여지를 남긴다(KNK-208).
    "deepseek-v4-flash": ResolvedModel(
        model="deepseek-v4-flash",
        provider=PROVIDER_DEEPSEEK,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=True,
        context_window_tokens=1_000_000,
        max_output_tokens=384_000,
        reasoning_effort=None,
        supported_reasoning_efforts=frozenset({"high", "max"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        capabilities_verified_on=date(2026, 7, 29),
        capabilities_source_urls=(
            "https://api-docs.deepseek.com/quick_start/pricing",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.14"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.0028"),
                output_usd_per_1m_tokens=Decimal("0.28"),
                source_url="https://api-docs.deepseek.com/quick_start/pricing",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 4, 24),
            ),
        ),
    ),
    "gpt-5.6-terra": ResolvedModel(
        model="gpt-5.6-terra",
        provider=PROVIDER_OPENAI,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=True,
        supports_temperature=False,
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_effort="medium",
        supported_reasoning_efforts=frozenset(
            {"none", "low", "medium", "high", "xhigh", "max"}
        ),
        structured_output_modes=frozenset(
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA}
        ),
        capabilities_verified_on=date(2026, 7, 29),
        capabilities_source_urls=(
            "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        ),
        # 공식 페이지에 이 이름과 다른 고정 스냅샷 ID가 아직 없다.
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2.50"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.25"),
                output_usd_per_1m_tokens=Decimal("15.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("3.125"),
                source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 7, 9),
                effective_until=date(2026, 7, 29),
                long_context_threshold_tokens=272_000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            ),
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.20"),
                output_usd_per_1m_tokens=Decimal("12.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("2.50"),
                source_url="https://developers.openai.com/api/docs/pricing",
                verified_on=date(2026, 8, 7),
                effective_from=date(2026, 7, 30),
                long_context_threshold_tokens=272_000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            ),
        ),
    ),
    "gpt-5.6-luna": ResolvedModel(
        model="gpt-5.6-luna",
        provider=PROVIDER_OPENAI,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=False,
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_effort="none",
        supported_reasoning_efforts=frozenset(
            {"none", "low", "medium", "high", "xhigh", "max"}
        ),
        structured_output_modes=frozenset(
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA}
        ),
        capabilities_verified_on=date(2026, 7, 29),
        capabilities_source_urls=(
            "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        ),
        # 공식 페이지에 이 이름과 다른 고정 스냅샷 ID가 아직 없다.
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("1.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.10"),
                output_usd_per_1m_tokens=Decimal("6.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("1.25"),
                source_url="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 7, 9),
                long_context_threshold_tokens=272_000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            ),
        ),
    ),
    "gpt-5.4-mini": ResolvedModel(
        model="gpt-5.4-mini",
        provider=PROVIDER_OPENAI,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=False,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
        reasoning_effort="none",
        supported_reasoning_efforts=frozenset(
            {"none", "low", "medium", "high", "xhigh"}
        ),
        structured_output_modes=frozenset(
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA}
        ),
        capabilities_verified_on=date(2026, 7, 29),
        capabilities_source_urls=(
            "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        ),
        snapshot_model="gpt-5.4-mini-2026-03-17",
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.75"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.075"),
                output_usd_per_1m_tokens=Decimal("4.50"),
                source_url="https://developers.openai.com/api/docs/models/gpt-5.4-mini",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 3, 17),
            ),
        ),
    ),
    "claude-sonnet-5": ResolvedModel(
        model="claude-sonnet-5",
        provider=PROVIDER_ANTHROPIC,
        adapter=ADAPTER_ANTHROPIC_SDK,
        use_thinking=False,
        supports_temperature=False,
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        # 현재 정책은 thinking을 끄고 effort도 보내지 않는다. 아래 목록은 나중에 thinking을 켤 때
        # 고를 수 있는 값이지, 지금 적용 중인 값이 아니다.
        reasoning_effort=None,
        supported_reasoning_efforts=frozenset(
            {"low", "medium", "high", "xhigh", "max"}
        ),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_SCHEMA}),
        capabilities_verified_on=date(2026, 7, 29),
        capabilities_source_urls=(
            "https://platform.claude.com/docs/en/about-claude/models/whats-new-sonnet-5",
            "https://platform.claude.com/docs/en/build-with-claude/structured-outputs",
        ),
        # Claude 4.6+의 dateless canonical ID는 공식적으로 고정 스냅샷이다.
        snapshot_model="claude-sonnet-5",
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.20"),
                output_usd_per_1m_tokens=Decimal("10.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("2.50"),
                source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 6, 30),
                effective_until=date(2026, 8, 31),
            ),
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("3.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.30"),
                output_usd_per_1m_tokens=Decimal("15.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("3.75"),
                source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                verified_on=date(2026, 7, 29),
                effective_from=date(2026, 9, 1),
            ),
        ),
    ),
    # 스토리 컴파일 대체 모델(KNK-951). terra급 체급(Intelligence 53~56)이면서 출력 속도가
    # 3배 이상 빠르고(~340 t/s vs ~104 t/s) 가격도 저렴하다. 추론 모드를 medium으로 쓴다.
    "gemini-3.7-flash": ResolvedModel(
        model="gemini-3.7-flash",
        provider=PROVIDER_GOOGLE,
        adapter=ADAPTER_GOOGLE_SDK,
        use_thinking=True,
        supports_temperature=True,
        context_window_tokens=1_048_576,
        max_output_tokens=65_536,
        reasoning_effort="medium",
        supported_reasoning_efforts=frozenset({"low", "medium", "high"}),
        structured_output_modes=frozenset(
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA}
        ),
        capabilities_verified_on=date(2026, 8, 21),
        capabilities_source_urls=(
            "https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.75"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.075"),
                output_usd_per_1m_tokens=Decimal("3.75"),
                source_url="https://ai.google.dev/gemini-api/docs/pricing",
                verified_on=date(2026, 8, 21),
                effective_from=date(2026, 8, 21),
                effective_until=date(2026, 12, 31),
            ),
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("1.50"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.15"),
                output_usd_per_1m_tokens=Decimal("7.50"),
                source_url="https://ai.google.dev/gemini-api/docs/pricing",
                verified_on=date(2026, 8, 21),
                effective_from=date(2027, 1, 1),
            ),
        ),
    ),
}


# 이 자리에 쓸 수 없는 공급자. 위반하면 서버 기동에서 막는다(`validate_selected_models`).
#
# **채팅 본문에 Anthropic을 막는 이유**(KNK-675): 채팅은 지시문(system)을 앞에 1개, 뒤에
# 2개(Depth·PHI) 놓는데, Anthropic은 요청 최상위의 지시문 칸이 하나뿐이라 앞의 1개만 옮기고
# 뒤의 둘은 버려진다(`anthropic_sdk._split_system`). 그중 PHI가 안전 가드레일이다. 즉 막지
# 않으면 **서버도 채팅도 정상으로 도는데 안전 지시만 빠진 채 호출된다** — 오류가 없어 한참
# 뒤에야 알게 된다. 직전까지는 "이 어댑터는 스트리밍을 못 한다"는 이유로 아래 검사에 걸렸지만,
# 조각 흘리기를 구현하면서(KNK-696) 그 그물이 사라졌다.
#
# 이 제약은 배치 문제를 푼 뒤에 풀린다. 대화 목록 안에 지시문 줄을 넣는 방법이 따로 있어
# (모델별 제약 있음 — `_split_system` 주석) 버리지 않을 길이 있고, 그 검토와 안전 실측이
# 채팅을 이 공급자로 여는 티켓의 몫이다. 그때 여기 한 줄을 지운다.
BLOCKED_PROVIDERS: dict[str, frozenset[str]] = {
    "CHAT_MODEL": frozenset({PROVIDER_ANTHROPIC}),
}


# 조각 흘리기(스트리밍)로 부르는 용도. 이 env로 고른 모델은 담당 어댑터가 스트리밍을 할 수
# 있어야 한다 — 못 하면 서버 기동에서 막는다(`llm.validate_startup`).
#
# **용도 이름이 여기에만 있는 것이 핵심이다.** 통로도 어댑터도 "채팅"을 몰라야 한다 — 용도에
# 맞춰 아래층을 깎으면 모델·공급자를 바꿀 때마다 그 층을 다시 고쳐야 한다(KNK-667 원칙).
# 등록부는 이미 env(=용도)와 모델을 잇는 자리라(`selected_models`) 여기가 제자리다.
STREAMING_ENVS = frozenset({"CHAT_MODEL"})


@dataclass(frozen=True)
class ProviderCredentials:
    """공급자 접속 정보. *_env는 값이 잘못됐을 때 무엇을 고치라고 알려줄 env 이름이다."""

    api_key: str
    base_url: str | None
    api_key_env: str
    base_url_env: str


def resolve(model: str) -> ResolvedModel:
    """모델 이름을 해석한다. 등록부에 없으면 LlmConfigError."""
    resolved = _REGISTRY.get(model)
    if resolved is None:
        known = ", ".join(sorted(_REGISTRY))
        raise LlmConfigError(f"등록되지 않은 모델 '{model}' — 등록된 모델: {known}")
    return resolved


def credentials(provider: str) -> ProviderCredentials:
    """공급자의 키·주소를 **호출 시점에** 읽는다.

    import 시점에 고정하지 않는 이유가 둘이다. 선택되지 않은 공급자의 키가 없어도 서버가
    떠야 하고(lazy), 런타임에 설정을 바꿔 넣는 테스트가 반영돼야 한다.
    """
    if provider == PROVIDER_DEEPSEEK:
        return ProviderCredentials(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_api_url,
            api_key_env="DEEPSEEK_API_KEY",
            base_url_env="DEEPSEEK_API_URL",
        )
    if provider == PROVIDER_OPENAI:
        return ProviderCredentials(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_url,
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_API_URL",
        )
    if provider == PROVIDER_ANTHROPIC:
        # base_url이 None이면 SDK 기본 주소를 쓴다 — 자체 호스팅 주소가 없는 게 보통이라
        # DeepSeek과 달리 기본값을 적어두지 않는다.
        return ProviderCredentials(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_api_url,
            api_key_env="ANTHROPIC_API_KEY",
            base_url_env="ANTHROPIC_API_URL",
        )
    if provider == PROVIDER_GOOGLE:
        return ProviderCredentials(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_api_url,
            api_key_env="GEMINI_API_KEY",
            base_url_env="GEMINI_API_URL",
        )
    raise LlmConfigError(f"공급자 '{provider}'의 접속 정보 규칙이 없습니다.")


def selected_models() -> tuple[tuple[str, str], ...]:
    """용도별로 선택된 모델을 (env 이름, 모델 이름) 쌍으로 돌려준다.

    env 이름을 함께 들고 다니는 이유가 둘이다. 기동 실패 메시지가 "무엇을 고쳐야 하는지"를
    가리켜야 하고, 용도별 제약(`BLOCKED_PROVIDERS`·`STREAMING_ENVS`)의 판정 단위가 이 env다.
    """
    return (
        ("STORYLINES_MODEL", settings.storylines_model),
        ("STORY_COMPILE_MODEL", settings.story_compile_model),
        ("CHAT_MODEL", settings.chat_model),
    )


def validate_selected_models() -> None:
    """선택된 모델이 등록돼 있고, 그 자리에 쓸 수 있는 공급자이며, 키가 채워졌는지 확인한다.
    위반 시 LlmConfigError.

    서버 시작 시 한 번 부른다 — 잘못 적은 모델 이름이나 빈 키는 첫 사용자 요청(502)이 아니라
    기동에서 드러나야 한다.

    **자리별 금지 공급자(`BLOCKED_PROVIDERS`)를 키·주소보다 먼저 본다.** 못 쓰는 공급자를
    꽂았는데 "키가 비어 있습니다"라고 답하면, 키만 채우면 될 것처럼 읽혀 엉뚱한 곳을 고치게 된다.

    주소(base_url)도 함께 본다. 키와 달리 주소는 **비어 있지 않아도 틀릴 수 있어**
    (`not-a-url`처럼) 검사를 통과한 뒤 첫 호출에서야 실패한다. 형식만 보는 것이라 실제 접속은
    하지 않는다 — 기동에 지연도 과금도 없다.
    """
    for env_name, model in selected_models():
        try:
            resolved = resolve(model)
            # 접속 정보 조회도 같이 감싼다 — 규칙이 없는 공급자면 여기서도 실패하는데,
            # 그때도 어느 env를 고칠지 메시지에 있어야 한다.
            creds = credentials(resolved.provider)
        except LlmConfigError as exc:
            raise LlmConfigError(f"{env_name}: {exc}") from exc
        if resolved.provider in BLOCKED_PROVIDERS.get(env_name, frozenset()):
            raise LlmConfigError(
                f"{env_name}={model}은 공급자 '{resolved.provider}'를 쓰는데, 이 자리는 그 "
                f"공급자를 쓸 수 없습니다(사유는 registry.BLOCKED_PROVIDERS 주석)."
            )
        if not creds.api_key.strip():
            raise LlmConfigError(
                f"{env_name}={model}은 공급자 '{resolved.provider}'를 쓰는데 "
                f"{creds.api_key_env}가 비어 있습니다."
            )
        if "\n" in creds.api_key or "\r" in creds.api_key:
            raise LlmConfigError(
                f"{env_name}={model}이 쓰는 {creds.api_key_env}에 개행이 있습니다."
            )
        if creds.api_key != creds.api_key.strip():
            raise LlmConfigError(
                f"{env_name}={model}이 쓰는 {creds.api_key_env}에 앞뒤 공백이 있습니다."
            )
        if not creds.api_key.isascii():
            raise LlmConfigError(
                f"{env_name}={model}이 쓰는 {creds.api_key_env}에 ASCII가 아닌 문자가 있습니다."
            )
        if not creds.api_key.isprintable() or any(ch.isspace() for ch in creds.api_key):
            raise LlmConfigError(
                f"{env_name}={model}이 쓰는 {creds.api_key_env}에 공백 또는 제어문자가 있습니다."
            )
        _validate_base_url(creds, env_name=env_name, model=model, provider=resolved.provider)


def _validate_base_url(
    creds: ProviderCredentials, *, env_name: str, model: str, provider: str
) -> None:
    """주소가 http/https이고 호스트가 있는지 본다. None은 통과 — SDK 기본 주소를 쓴다는 뜻이다."""
    if creds.base_url is None:
        return
    parsed = urlparse(creds.base_url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return
    raise LlmConfigError(
        f"{env_name}={model}이 쓰는 공급자 '{provider}'의 {creds.base_url_env}가 "
        f"올바른 주소가 아닙니다(http:// 또는 https://와 호스트가 필요): {creds.base_url!r}"
    )
