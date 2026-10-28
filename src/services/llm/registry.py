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
    ADAPTER_TYPESAFE_API,
    PROVIDER_ALIBABA,
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    PROVIDER_GOOGLE,
    PROVIDER_MOONSHOT,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_TYPESAFE,
    PROVIDER_ZAI,
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
    "jev-1.13.0": ResolvedModel(
        model="jev-1.13.0",
        provider=PROVIDER_TYPESAFE,
        adapter=ADAPTER_TYPESAFE_API,
        use_thinking=False,
        supports_temperature=False,
        context_window_tokens=64_000,
        # 선택형 판정 전용. 텍스트 생성 한도·추론 설정을 지어내지 않는다.
        capabilities_verified_on=date(2026, 9, 28),
        capabilities_source_urls=("https://docs.typesafe.ai/models", "https://docs.typesafe.ai/api"),
        snapshot_model="jev-1.13.0",
        pricing=(ModelPricing(
            input_usd_per_1m_tokens=Decimal("0.042"),
            cache_read_input_usd_per_1m_tokens=Decimal("0.042"),
            output_usd_per_1m_tokens=Decimal("0"),
            source_url="https://docs.typesafe.ai/models",
            verified_on=date(2026, 9, 28),
        ),),
    ),
    # 스토리라인·선택지(STORYLINES_MODEL·CHAT_CHOICE_MODEL)의
    # 기본값. 비추론 호출 — 창작 태스크에서 추론 모드가
    # 출력 외국어 오염·평면화를 일으켜 비추론이 더 안정적이었다(KNK-208 벤치).
    #
    # DeepSeek은 2026-09-10에 `deepseek-v4-flash`를 `deepseek-flash`(V4.1 Flash)로 바꿨다(KNK-1195).
    # 옛 이름은 alias로 남아 호출은 되지만 응답 모델명이 `deepseek-flash`로 돌아와 요청·응답 이름이
    # 어긋나므로 등록하지 않는다. `deepseek-v4-pro`도 2026-09-14부터 V4.1 Flash로 라우팅돼 지웠다.
    "deepseek-flash": ResolvedModel(
        model="deepseek-flash",
        provider=PROVIDER_DEEPSEEK,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=True,
        context_window_tokens=1_000_000,
        max_output_tokens=384_000,
        reasoning_effort=None,
        # none은 추론 끔, low·high·max는 실제로 다른 단계다. medium·xhigh는 회사가 high로 바꾸는
        # 별칭이라 넣지 않는다(같은 설정을 두 번 돌리게 된다, 2026-10-16 문서 확인).
        supported_reasoning_efforts=frozenset({"none", "low", "high", "max"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        # 이미지 입력: 가격표의 "Vision ✓"와 Vision 가이드(JPEG·PNG·GIF·WebP, 장당 32MiB)로
        # 확인했다(KNK-1359). 게시물 검수의 대체 모델이라 이 값이 없으면 대체 호출이 이미지를
        # 못 본다.
        supports_image_input=True,
        capabilities_verified_on=date(2026, 9, 23),
        capabilities_source_urls=(
            "https://api-docs.deepseek.com/quick_start/pricing",
            "https://api-docs.deepseek.com/guides/vision",
        ),
        snapshot_model=None,
        # 단가는 시간대에 따라 둘이다 — 피크(UTC 월~금 01:00~04:00·06:00~10:00)와 그 절반인 오프피크.
        # 이 표는 날짜 구간만 담으므로 **피크 값**을 적는다(비용을 적게 잡는 것보다 안전). 실제 시간대별
        # 비용 계산은 Langfuse가 한다 — 호출마다 `deepseek_pricing`이 붙인 metadata를 보고
        # 단가표의 피크·오프피크 구간을 고른다(KNK-1195).
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.30"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.006"),
                output_usd_per_1m_tokens=Decimal("1.20"),
                source_url="https://api-docs.deepseek.com/quick_start/pricing",
                verified_on=date(2026, 9, 11),
                effective_from=date(2026, 9, 10),
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
        # 이미지 입력: 모델 페이지의 "Input modalities: text, image"로 확인했다(KNK-1359).
        supports_image_input=True,
        capabilities_verified_on=date(2026, 9, 23),
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
                effective_until=date(2026, 7, 29),
                long_context_threshold_tokens=272_000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            ),
            # 2026-07-30부터 80% 인하(OpenAI changelog). KNK-1412에서 뒤늦게 확인해 반영했다.
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.20"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.02"),
                output_usd_per_1m_tokens=Decimal("1.20"),
                cache_write_input_usd_per_1m_tokens=Decimal("0.25"),
                source_url="https://developers.openai.com/api/docs/pricing",
                verified_on=date(2026, 9, 23),
                effective_from=date(2026, 7, 30),
                long_context_threshold_tokens=272_000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            ),
        ),
    ),
    # 채팅 라이트 모델(KNK-1411). 등록부 기본 강도는 none이고 본문 라우팅은 low를 지정한다.
    # temperature는 none일 때만 받지만, 추론 단계를 올리면 400이 나므로 받지 않는 것으로 적는다.
    "gpt-6-luna": ResolvedModel(
        model="gpt-6-luna",
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
        # 게시물 검수의 기본 모델. 공식 모델 문서에서 이미지 입력 지원을 확인함.
        supports_image_input=True,
        capabilities_verified_on=date(2026, 9, 30),
        capabilities_source_urls=(
            "https://developers.openai.com/api/docs/models/gpt-6-luna",
        ),
        # 공식 페이지의 스냅샷이 `gpt-6-luna` 하나뿐이라 이름과 다른 고정 스냅샷 ID가 없다.
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.10"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.01"),
                output_usd_per_1m_tokens=Decimal("0.50"),
                cache_write_input_usd_per_1m_tokens=Decimal("0.125"),
                source_url="https://developers.openai.com/api/docs/models/gpt-6-luna",
                verified_on=date(2026, 9, 23),
                effective_from=date(2026, 9, 22),
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
        # 출시 때 8/31까지의 할인가로 발표된 $2/$10이 정가로 바뀌었다. 예정됐던 9/1 인상($3/$15)은
        # 취소됐다(공식 가격 페이지, 2026-09-23 재확인, KNK-1414).
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.20"),
                output_usd_per_1m_tokens=Decimal("10.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("2.50"),
                source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                verified_on=date(2026, 9, 23),
                effective_from=date(2026, 6, 30),
            ),
        ),
    ),
    # 스토리 컴파일 대체 모델(KNK-951). terra급 체급이면서 출력 속도가
    # 3배 이상 빠르고 가격도 저렴하다. 추론 모드를 medium으로 쓴다.
    # 3.6은 서버 안정성이 확보된 모델, 3.7은 더 빠르지만 출시 직후라 503이 잦을 수 있다.
    "gemini-3.6-flash": ResolvedModel(
        model="gemini-3.6-flash",
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
        capabilities_verified_on=date(2026, 8, 22),
        capabilities_source_urls=(
            "https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.75"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.075"),
                output_usd_per_1m_tokens=Decimal("3.75"),
                source_url="https://ai.google.dev/gemini-api/docs/pricing",
                verified_on=date(2026, 8, 22),
                effective_from=date(2026, 8, 22),
                effective_until=date(2026, 12, 31),
            ),
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("1.50"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.15"),
                output_usd_per_1m_tokens=Decimal("7.50"),
                source_url="https://ai.google.dev/gemini-api/docs/pricing",
                verified_on=date(2026, 8, 22),
                effective_from=date(2027, 1, 1),
            ),
        ),
    ),
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
    # ── 채팅 grid search 후보(2026-10-16 추가) ─────────────────────────────────────
    # 기본 추론 강도는 none~high 중 **이 모델이 받는 가장 낮은 값**으로 둔다(none이 되면 none, 아니면
    # low). 본문 라우팅은 호출에서 강도를 따로 지정하므로, 기본값은 고르지 않았을 때
    # 비용·지연이 가장 적은 쪽이 안전하다.
    # Haiku 5.5는 추론을 끌 수 있다(effort high 이하일 때만 — 끌 때는 effort를 보내지 않아 기본
    # medium으로 간다). 4.6 이후 모델 중 유일하게 프롬프트 길이별 요금이 있다(10만 토큰 초과 시 5배).
    "claude-haiku-5-5": ResolvedModel(
        model="claude-haiku-5-5",
        provider=PROVIDER_ANTHROPIC,
        adapter=ADAPTER_ANTHROPIC_SDK,
        use_thinking=False,
        # 5.5 세대는 기본값이 아닌 temperature·top_p·top_k를 보내면 매 요청 400이다.
        supports_temperature=False,
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_effort=None,
        supported_reasoning_efforts=frozenset(
            {"none", "low", "medium", "high", "xhigh", "max"}
        ),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_SCHEMA}),
        # 대화 중간 지시문(`role: "system"`)을 공식 지원한다(beta 헤더 불필요).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://platform.claude.com/docs/en/about-claude/models/overview",
            "https://platform.claude.com/docs/en/build-with-claude/effort",
            "https://platform.claude.com/docs/en/build-with-claude/thinking",
            "https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages",
        ),
        # 날짜 없는 ID가 그 자체로 고정 스냅샷이다(공식 모델 개요).
        snapshot_model="claude-haiku-5-5",
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.10"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.01"),
                output_usd_per_1m_tokens=Decimal("0.50"),
                cache_write_input_usd_per_1m_tokens=Decimal("0.125"),
                source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                verified_on=date(2026, 10, 16),
                long_context_threshold_tokens=100_000,
                long_context_input_multiplier=Decimal("5"),
                long_context_output_multiplier=Decimal("5"),
            ),
        ),
    ),
    # Sonnet 5.5는 none을 두지 않는다(사용자 결정, 2026-10-16). `disabled`는 400이고, 문서가 대신 권하는
    # `between_tools`(도구 없으면 글만 낸다)는 시운전에서 첫 글자 15초·답 길이 대비 토큰 약 2배로 나와
    # 실제로 생각하지 않는다고 믿기 어려웠다. none으로 적고 생각한 결과를 섞지 않으려고 low부터 연다.
    "claude-sonnet-5-5": ResolvedModel(
        model="claude-sonnet-5-5",
        provider=PROVIDER_ANTHROPIC,
        adapter=ADAPTER_ANTHROPIC_SDK,
        use_thinking=True,
        # 5.5 세대는 기본값이 아닌 temperature·top_p·top_k를 보내면 매 요청 400이다.
        supports_temperature=False,
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_effort="low",
        supported_reasoning_efforts=frozenset(
            {"low", "medium", "high", "xhigh", "max"}
        ),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_SCHEMA}),
        # 대화 중간 지시문(`role: "system"`)을 공식 지원한다(beta 헤더 불필요).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://platform.claude.com/docs/en/about-claude/models/overview",
            "https://platform.claude.com/docs/en/build-with-claude/effort",
            "https://platform.claude.com/docs/en/build-with-claude/thinking",
            "https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages",
        ),
        # 날짜 없는 ID가 그 자체로 고정 스냅샷이다(공식 모델 개요).
        snapshot_model="claude-sonnet-5-5",
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.10"),
                output_usd_per_1m_tokens=Decimal("10"),
                cache_write_input_usd_per_1m_tokens=Decimal("2.50"),
                source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                verified_on=date(2026, 10, 16),
            ),
        ),
    ),
    # Opus 5.5는 추론을 끌 수 없다("Adaptive (always on)", disabled·between_tools 모두 400). none 없음.
    "claude-opus-5-5": ResolvedModel(
        model="claude-opus-5-5",
        provider=PROVIDER_ANTHROPIC,
        adapter=ADAPTER_ANTHROPIC_SDK,
        use_thinking=True,
        # 5.5 세대는 기본값이 아닌 temperature·top_p·top_k를 보내면 매 요청 400이다.
        supports_temperature=False,
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_effort="low",
        supported_reasoning_efforts=frozenset(
            {"low", "medium", "high", "xhigh", "max"}
        ),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_SCHEMA}),
        # 대화 중간 지시문(`role: "system"`)을 공식 지원한다(beta 헤더 불필요).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://platform.claude.com/docs/en/about-claude/models/overview",
            "https://platform.claude.com/docs/en/build-with-claude/effort",
            "https://platform.claude.com/docs/en/build-with-claude/thinking",
            "https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages",
        ),
        # 날짜 없는 ID가 그 자체로 고정 스냅샷이다(공식 모델 개요).
        snapshot_model="claude-opus-5-5",
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("4"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.20"),
                output_usd_per_1m_tokens=Decimal("20"),
                cache_write_input_usd_per_1m_tokens=Decimal("5"),
                source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                verified_on=date(2026, 10, 16),
            ),
        ),
    ),
    # gpt-6.1-sol은 none·minimal을 받지 않는다(기본 medium). none이 아니면 temperature를 빼라는
    # 문서라 temperature를 받지 않는 것으로 적는다.
    "gpt-6.1-sol": ResolvedModel(
        model="gpt-6.1-sol",
        provider=PROVIDER_OPENAI,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=True,
        supports_temperature=False,
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_effort="low",
        supported_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
        structured_output_modes=frozenset(
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA}
        ),
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
            "https://developers.openai.com/api/docs/guides/reasoning",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.10"),
                output_usd_per_1m_tokens=Decimal("10.00"),
                cache_write_input_usd_per_1m_tokens=Decimal("2.50"),
                source_url="https://developers.openai.com/api/docs/pricing",
                verified_on=date(2026, 10, 16),
                long_context_threshold_tokens=272_000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            ),
        ),
    ),
    # gemini-3.8-flash는 추론을 끌 수 없다(low·medium·high만, minimal은 오류). temperature는 문서가
    # "deprecated, 빼라"고 해 받지 않는 것으로 적는다. 3.7-flash는 이제 3.8로 자동 연결된다.
    "gemini-3.8-flash": ResolvedModel(
        model="gemini-3.8-flash",
        provider=PROVIDER_GOOGLE,
        adapter=ADAPTER_GOOGLE_SDK,
        use_thinking=True,
        supports_temperature=False,
        context_window_tokens=1_048_576,
        max_output_tokens=65_536,
        reasoning_effort="low",
        supported_reasoning_efforts=frozenset({"low", "medium", "high"}),
        structured_output_modes=frozenset(
            {STRUCTURED_OUTPUT_JSON_OBJECT, STRUCTURED_OUTPUT_JSON_SCHEMA}
        ),
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash",
            "https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.8",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.75"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.075"),
                output_usd_per_1m_tokens=Decimal("3.75"),
                source_url="https://ai.google.dev/gemini-api/docs/pricing",
                verified_on=date(2026, 10, 16),
                effective_until=date(2026, 12, 31),
            ),
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("1.50"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.15"),
                output_usd_per_1m_tokens=Decimal("7.50"),
                source_url="https://ai.google.dev/gemini-api/docs/pricing",
                verified_on=date(2026, 10, 16),
                effective_from=date(2027, 1, 1),
            ),
        ),
    ),
    # ── 오픈소스 계열 후보(KNK-1628) ─────────────────────────────────────────────
    # 채팅 configuration 비교(grid search) 후보다. 공급자 공식 문서만으로 적었고 실제 응답으로는
    # 확인하지 않았다. 추론 강도 목록에는 **실제로 다르게 동작하는 값만** 적는다 — 공급자가 다른
    # 값으로 바꿔 받는 별칭(예: GLM의 low·medium → high)을 넣으면 같은 설정을 두 번 돌리게 된다.
    # json_object 지원은 문서로 확인하지 않았다(본문 생성은 JSON을 쓰지 않는다). JSON을 쓰는 자리에
    # 고르기 전에 확인한다. 이미지 입력도 검수에 쓸 일이 없어 기본값(받지 않음)으로 둔다.
    # Qwen3.8 텍스트 모델은 max·flash 둘이다. 명세의 "qwen-3.8"이 어느 쪽인지 정해지지 않아 둘 다 둔다.
    # Alibaba 직접 대신 OpenRouter로 부른다(키가 OpenRouter 것이다, 사용자 결정). OpenRouter는
    # `회사/모델` 이름을 받아 `model`을 그 이름으로 적고, 등록부 이름(env에 쓰는 이름)은 그대로 둔다.
    # 추론 제어는 OpenRouter의 `reasoning.effort`다 — 회사 문법 변환은 어댑터가 한다.
    #
    # max는 추론을 끌 수 없다(카탈로그 `reasoning.mandatory: true`, none을 보내면 거부). 아무것도 안
    # 보내면 xhigh로 돌아 가장 비싸므로 기본값을 low로 명시한다. 평문 `qwen/qwen3.8-max`는 카탈로그에
    # 없어 고정판 이름을 쓴다.
    "qwen3.8-max": ResolvedModel(
        model="qwen/qwen3.8-max-0902",
        provider=PROVIDER_OPENROUTER,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=True,
        supports_temperature=True,
        context_window_tokens=1_000_000,
        max_output_tokens=131_072,
        reasoning_effort="low",
        supported_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        # 대화 끝 지시문을 받는다 — 문서로는 확인하지 못했고 실측으로 확인했다(2026-10-16, 끝 지시문에 둔
        # 출력 규칙을 3회 중 2~3회 따름).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://openrouter.ai/api/v1/models/qwen/qwen3.8-max-0902/endpoints",
            "https://openrouter.ai/docs/guides/best-practices/reasoning-tokens",
        ),
        snapshot_model="qwen/qwen3.8-max-0902",
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("2"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.25"),
                output_usd_per_1m_tokens=Decimal("6"),
                source_url="https://openrouter.ai/qwen/qwen3.8-max-0902",
                verified_on=date(2026, 10, 16),
            ),
        ),
    ),
    # flash는 추론을 끌 수 있지만 기본이 켜짐이라, 끌 때도 none을 명시해 보낸다. 강도 단계가 따로
    # 없어 OpenRouter가 low·medium·high·xhigh를 max_tokens의 20·50·80·95% 추론 예산으로 바꾼다 —
    # 값마다 예산이 달라 실제로 다르게 동작하므로 목록에 둔다.
    "qwen3.8-flash": ResolvedModel(
        model="qwen/qwen3.8-flash",
        provider=PROVIDER_OPENROUTER,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=True,
        context_window_tokens=1_000_000,
        max_output_tokens=131_072,
        reasoning_effort=None,
        supported_reasoning_efforts=frozenset({"none", "low", "medium", "high", "xhigh"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        # 대화 끝 지시문을 받는다 — 문서로는 확인하지 못했고 실측으로 확인했다(2026-10-16, 끝 지시문에 둔
        # 출력 규칙을 3회 중 2~3회 따름).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 16),
        capabilities_source_urls=(
            "https://openrouter.ai/api/v1/models/qwen/qwen3.8-flash/endpoints",
            "https://openrouter.ai/docs/guides/best-practices/reasoning-tokens",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("0.15"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.016"),
                output_usd_per_1m_tokens=Decimal("0.47"),
                source_url="https://openrouter.ai/qwen/qwen3.8-flash",
                verified_on=date(2026, 10, 16),
            ),
        ),
    ),
    # GLM-5.2가 실제로 구분하는 강도는 high·max 둘뿐이다(low·medium → high, xhigh → max).
    # temperature 범위가 [0, 1]이라 1을 넘는 값을 보내면 거부된다.
    "glm-5.2": ResolvedModel(
        model="glm-5.2",
        provider=PROVIDER_ZAI,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=True,
        context_window_tokens=1_000_000,
        max_output_tokens=131_072,
        reasoning_effort=None,
        supported_reasoning_efforts=frozenset({"none", "high", "max"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        # 대화 끝 지시문을 받는다 — 문서로는 확인하지 못했고 실측으로 확인했다(2026-10-16, 끝 지시문에 둔
        # 출력 규칙을 3회 중 2~3회 따름).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 12),
        capabilities_source_urls=(
            "https://docs.z.ai/guides/llm/glm-5.2",
            "https://docs.z.ai/api-reference/llm/chat-completion",
            "https://docs.z.ai/guides/capabilities/thinking-mode",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("1.4"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.26"),
                output_usd_per_1m_tokens=Decimal("4.4"),
                source_url="https://docs.z.ai/guides/overview/pricing",
                verified_on=date(2026, 10, 12),
            ),
        ),
    ),
    # Kimi K3는 추론을 끌 수 없고(어댑터가 끄라는 설정을 거부한다) temperature는 1.0 고정이라 보내면
    # 오류다. 여러 턴 대화에서는 응답의 추론 내용까지 되돌려 보내라고 권하지만, 대화 기록에는 본문만
    # 남아 그렇게 하지 못한다 — 품질에 영향이 있을 수 있다.
    "kimi-k3": ResolvedModel(
        model="kimi-k3",
        provider=PROVIDER_MOONSHOT,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=True,
        supports_temperature=False,
        context_window_tokens=1_048_576,
        max_output_tokens=1_048_576,
        reasoning_effort="low",
        supported_reasoning_efforts=frozenset({"low", "high", "max"}),
        structured_output_modes=frozenset({STRUCTURED_OUTPUT_JSON_OBJECT}),
        # 대화 끝 지시문을 받는다 — 문서로는 확인하지 못했고 실측으로 확인했다(2026-10-16, 끝 지시문에 둔
        # 출력 규칙을 3회 중 2~3회 따름).
        supports_mid_conversation_system=True,
        capabilities_verified_on=date(2026, 10, 12),
        capabilities_source_urls=(
            "https://platform.kimi.ai/docs/api/chat",
            "https://platform.kimi.ai/docs/api/models-overview",
            "https://platform.kimi.ai/docs/guide/kimi-k3-quickstart",
        ),
        snapshot_model=None,
        pricing=(
            ModelPricing(
                input_usd_per_1m_tokens=Decimal("3.00"),
                cache_read_input_usd_per_1m_tokens=Decimal("0.30"),
                output_usd_per_1m_tokens=Decimal("15.00"),
                source_url="https://platform.kimi.ai/docs/pricing/chat",
                verified_on=date(2026, 10, 12),
                # 기본 5분 보관 캐시의 쓰기 단가.
                cache_write_input_usd_per_1m_tokens=Decimal("3.00"),
            ),
        ),
    ),
}


# 이 자리에 쓸 수 없는 공급자. 위반하면 서버 기동에서 막는다(`validate_selected_models`).
#
# **선택지에 Anthropic을 막는 이유**(KNK-1416). 이 회사는
# JSON 강제에 스키마 전체를 요구하는데 통로에 그 칸이 없어 어댑터가 인자를 빼고 보낸다
# (`anthropic_sdk`). 형식 준수가 프롬프트에만 기대게 되므로 별도 검토 전에는 열지 않는다.
BLOCKED_PROVIDERS: dict[str, frozenset[str]] = {
    "CHAT_CHOICE_MODEL": frozenset({PROVIDER_ANTHROPIC}),
    "MODERATION_MODEL": frozenset({PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC, PROVIDER_GOOGLE}),
    "MODERATION_FALLBACK_MODEL": frozenset({PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_GOOGLE}),
}

# 조각 흘리기(스트리밍)로 부르는 용도. 이 자리에 고른 모델은 담당 어댑터가 스트리밍을 할 수
# 있어야 한다 — 못 하면 서버 기동에서 막는다(`llm.validate_startup`).
#
# **용도 이름이 여기에만 있는 것이 핵심이다.** 통로도 어댑터도 "채팅"을 몰라야 한다 — 용도에
# 맞춰 아래층을 깎으면 모델·공급자를 바꿀 때마다 그 층을 다시 고쳐야 한다(KNK-667 원칙).
# 등록부는 이미 env(=용도)와 모델을 잇는 자리라(`selected_models`) 여기가 제자리다.
# 환경 변수가 아닌 코드 내 라우팅 대상. 호출과 기동 검사가 같은 모델·강도를 사용한다.
ROUTING_TARGETS: dict[str, tuple[str, str]] = {
    "CHAT_LITE": ("gpt-6-luna", "low"),
    "CHAT_AURORA_EVEN": ("gpt-6.1-sol", "low"),
    "CHAT_AURORA_ODD": ("gemini-3.8-flash", "medium"),
}
STREAMING_SLOTS = frozenset(ROUTING_TARGETS)

# 비스트리밍 요약의 기본·폴백 모델. 호출과 기동 검사에서 함께 사용한다.
COMPACT_MODELS = ("claude-sonnet-5-5", "deepseek-flash")


def blocked_providers(env_name: str) -> frozenset[str]:
    """이 자리(env)에 쓸 수 없는 공급자를 반환한다."""
    return BLOCKED_PROVIDERS.get(env_name, frozenset())


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
    if provider == PROVIDER_TYPESAFE:
        return ProviderCredentials(
            api_key=settings.typesafe_api_key,
            base_url=settings.typesafe_api_url,
            api_key_env="TYPESAFE_API_KEY",
            base_url_env="TYPESAFE_API_URL",
        )
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
    if provider == PROVIDER_ALIBABA:
        return ProviderCredentials(
            api_key=settings.alibaba_api_key,
            base_url=settings.alibaba_api_url,
            api_key_env="ALIBABA_API_KEY",
            base_url_env="ALIBABA_API_URL",
        )
    if provider == PROVIDER_ZAI:
        return ProviderCredentials(
            api_key=settings.zai_api_key,
            base_url=settings.zai_api_url,
            api_key_env="ZAI_API_KEY",
            base_url_env="ZAI_API_URL",
        )
    if provider == PROVIDER_MOONSHOT:
        return ProviderCredentials(
            api_key=settings.moonshot_api_key,
            base_url=settings.moonshot_api_url,
            api_key_env="MOONSHOT_API_KEY",
            base_url_env="MOONSHOT_API_URL",
        )
    if provider == PROVIDER_OPENROUTER:
        return ProviderCredentials(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_api_url,
            api_key_env="OPENROUTER_API_KEY",
            base_url_env="OPENROUTER_API_URL",
        )
    raise LlmConfigError(f"공급자 '{provider}'의 접속 정보 규칙이 없습니다.")


def selected_models() -> tuple[tuple[str, str], ...]:
    """용도별로 선택된 모델을 (설정 또는 라우팅 자리 이름, 모델 이름) 쌍으로 돌려준다.

    자리 이름을 함께 들고 다니는 이유가 둘이다. 기동 실패 메시지가 "무엇을 고쳐야 하는지"를
    가리켜야 하고, 용도별 제약(`BLOCKED_PROVIDERS`·`STREAMING_SLOTS`)의 판정 단위가 이 자리다.
    """
    return (
        ("STORYLINES_MODEL", settings.storylines_model),
        ("STORY_COMPILE_MODEL", settings.story_compile_model),
        *((slot, model) for slot, (model, _) in ROUTING_TARGETS.items()),
        ("CHAT_CHOICE_MODEL", settings.chat_choice_model),
        ("CHAT_COMPACT_MODEL", COMPACT_MODELS[0]),
        ("CHAT_COMPACT_FALLBACK_MODEL", COMPACT_MODELS[1]),
        ("MODERATION_MODEL", settings.moderation_model),
        ("MODERATION_FALLBACK_MODEL", settings.moderation_fallback_model),
        ("JEV_MODEL", settings.jev_model),
    )


def validate_selected_models() -> None:
    """선택된 모델이 등록돼 있고, 그 자리에 쓸 수 있는 공급자이며, 키가 채워졌는지 확인한다.
    위반 시 LlmConfigError.

    서버 시작 시 한 번 부른다. 검수에만 필요한 키의 누락은 해당 검수 호출에서 처리하며,
    나머지 설정 오류는 기동에서 드러낸다.

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
        if (env_name == "JEV_MODEL") != (resolved.adapter == ADAPTER_TYPESAFE_API):
            raise LlmConfigError(f"{env_name}: 텍스트 생성 모델과 판정 전용 모델은 서로 바꿀 수 없습니다.")
        if resolved.provider in blocked_providers(env_name):
            raise LlmConfigError(
                f"{env_name}={model}은 공급자 '{resolved.provider}'를 쓰는데, 이 자리는 그 "
                f"공급자를 쓸 수 없습니다(사유는 registry.BLOCKED_PROVIDERS 주석)."
            )
        effort = ROUTING_TARGETS[env_name][1] if env_name in ROUTING_TARGETS else None
        if effort is not None and effort not in resolved.supported_reasoning_efforts:
            # 첫 채팅에서야 실패하지 않게 기동에서 막는다. 회사가 받지 않는 강도(예: 추론을 끌 수 없는
            # 모델의 none)는 비슷한 값으로 바꾸지 않는다 — 결과표에 다른 설정이 적히게 된다.
            raise LlmConfigError(
                f"{env_name}={model}이 추론 강도 {effort}를 받지 않습니다 — "
                f"받는 값: {sorted(resolved.supported_reasoning_efforts)}"
            )
        if env_name in {"MODERATION_MODEL", "MODERATION_FALLBACK_MODEL"}:
            mode = STRUCTURED_OUTPUT_JSON_SCHEMA if env_name == "MODERATION_MODEL" else STRUCTURED_OUTPUT_JSON_OBJECT
            if not resolved.supports_image_input or mode not in resolved.structured_output_modes:
                raise LlmConfigError(f"{env_name}={model}은 이미지 입력과 {mode} 출력을 지원해야 합니다.")
            if env_name == "MODERATION_MODEL" and "high" not in resolved.supported_reasoning_efforts:
                raise LlmConfigError(f"{env_name}={model}은 high 추론 강도를 지원해야 합니다.")
        if not creds.api_key.strip():
            # 검수·JEV 키 누락은 해당 호출에서 처리한다. 다른 기능의 기동을 막지 않는다.
            if env_name in {"MODERATION_MODEL", "MODERATION_FALLBACK_MODEL", "JEV_MODEL"}:
                continue
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
