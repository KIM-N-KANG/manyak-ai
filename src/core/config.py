from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 채팅 본문 프롬프트 종류(KNK-1622). layer는 6레이어 조립, general은 파일 하나 조립이다.
ChatPrompt = Literal["layer", "general"]


class Settings(BaseSettings):
    # extra="ignore": .env에 남은 옛/무관 키(예: 폐기된 DEEPSEEK_MODEL·DEEPSEEK_CHAT_MODEL)를
    # 기동 실패로 만들지 않는다. pydantic-settings는 dotenv 여분 키를 기본 금지(extra_forbidden)해
    # 필드 rename(KNK-595) 뒤 옛 .env로 앱이 안 뜨는 문제가 있었다 — 프로세스 env는 원래 무시하므로
    # dotenv도 같게 맞춘다.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Service"
    app_version: str = "0.4.5"
    debug: bool = False

    # KNK-1554: Langfuse와 분리된 인프라 추적. endpoint는 /v1/traces까지 포함합니다.
    manyak_tracing_enabled: bool = False
    manyak_otlp_traces_endpoint: str = ""

    deepseek_api_key: str
    deepseek_api_url: str = "https://api.deepseek.com"
    # 대체 공급자 접속 정보(KNK-703). 기동 검사는 *선택된* 모델의 공급자 키만 본다.
    # 현재 기본값은 컴파일에 OpenAI, 스토리라인·채팅에 DeepSeek을 쓰므로 두 키가 모두 필요하다.
    # 주소 기본값은 None — "빈 문자열"이 아니라 "SDK 기본 주소를 쓴다"는 뜻이다. 빈 문자열로
    # 두면 기동 검사의 주소 형식 검사에 걸린다.
    openai_api_key: str = ""
    openai_api_url: str | None = None
    # Anthropic 접속 정보(KNK-675).
    # 주소 기본값은 None — "빈 문자열"이 아니라 "SDK 기본 주소를 쓴다"는 뜻이다. 빈 문자열로
    # 두면 기동 검사의 주소 형식 검사에 걸린다.
    anthropic_api_key: str = ""
    anthropic_api_url: str | None = None
    # Google 접속 정보(KNK-951).
    # 주소 기본값은 None — SDK 기본 주소를 쓴다는 뜻이다.
    gemini_api_key: str = ""
    gemini_api_url: str | None = None
    # 오픈소스 계열 공급자 접속 정보(KNK-1628). OpenAI SDK로 부르므로 주소를 비우면 SDK 기본값인
    # OpenAI 주소로 키가 나간다 — 그래서 None이 아니라 각 회사의 OpenAI 호환 주소를 기본값으로 둔다.
    # 키는 원래 이름이 없으면 로컬 .env의 개발용 칸(*_DEV)을 읽는다. 이 셋은 grid search에만 쓰여
    # 운영 키가 없고, 같은 값을 원래 이름에 또 복사해 두지 않으려는 것이다(사용자 결정).
    alibaba_api_key: str = Field(
        default="", repr=False, validation_alias=AliasChoices("ALIBABA_API_KEY", "ALIBABA_API_KEY_DEV")
    )
    alibaba_api_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    zai_api_key: str = Field(
        default="", repr=False, validation_alias=AliasChoices("ZAI_API_KEY", "ZAI_API_KEY_DEV")
    )
    zai_api_url: str = "https://api.z.ai/api/paas/v4"
    moonshot_api_key: str = Field(
        default="", repr=False, validation_alias=AliasChoices("MOONSHOT_API_KEY", "MOONSHOT_API_KEY_DEV")
    )
    moonshot_api_url: str = "https://api.moonshot.ai/v1"
    # OpenRouter(Qwen 중계). 위 셋과 같은 이유로 회사 주소를 기본값에, 개발용 칸(*_DEV)을 대체 키로 둔다.
    openrouter_api_key: str = Field(
        default="", repr=False, validation_alias=AliasChoices("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_DEV")
    )
    openrouter_api_url: str = "https://openrouter.ai/api/v1"
    # JEV 판정(KNK-1448). 키 부재는 서버 기동 대신 해당 호출에서 드러낸다.
    typesafe_api_key: str = Field(default="", repr=False)
    typesafe_api_url: str = "https://api.typesafe.ai"
    jev_model: str = "jev-1.13.0"
    # 모델은 용도별 env var로 분리한다(KNK-595). 스토리라인·채팅은 지금은 같은 flash 기본이지만
    # 독립적으로 바꿀 수 있도록 필드를 나눴다. manyak-infra의 Compose env 이름도 같이 맞춘다.
    story_compile_model: str = "gpt-5.6-terra"  # 스토리 컴파일 전용
    storylines_model: str = "deepseek-flash"  # 스토리라인 생성 전용(fast, KNK-215)
    chat_model: str = "deepseek-flash"  # 채팅 본문 전용, 판정 모델이 비면 판정도 맡음(fast, KNK-215)
    # 판정은 본문과 따로 고를 수 있다(KNK-1620). 본문 모델을 바꿔 비교할 때 판정까지 딸려가지
    # 않게 하려는 것이다. 선택지와 달리 기본값을 박지 않고 비워 두면 CHAT_MODEL을 따른다 —
    # 운영 CHAT_MODEL은 배포 설정으로 바뀔 수 있어, 기본값을 따로 두면 판정만 몰래 옛 모델로
    # 돌아갈 수 있기 때문이다. 실제로 쓸 값은 `effective_chat_judgement_model`로 읽는다.
    chat_judgement_model: str = ""
    # 채팅 본문 프롬프트를 고른다(KNK-1622). 기본 layer라 env가 없으면 운영 동작은 그대로다.
    # 다른 값이면 Settings 생성에서 검증 오류가 나 서버가 뜨지 않는다.
    chat_prompt: ChatPrompt = "layer"
    # 채팅 본문의 추론 강도(none·low·medium·high 등). 비우면 등록부에 적힌 그 모델의 기본값을 쓴다 —
    # env가 없으면 운영 동작은 그대로다. 모델마다 받는 값이 달라, 고른 모델이 받지 않는 값이면
    # 서버 기동에서 막는다(`registry.validate_selected_models`). 모델 비교 실험용이다.
    chat_reasoning_effort: str = ""
    # 선택지는 본문과 따로 고른다(KNK-1416). 본문 모델을 바꿔도 별도 API인 선택지가 딸려가지
    # 않게 하려는 것이다. 운영 env가 없으면 이 기본값이 쓰이므로 분리 전 운영과 같은 모델로 둔다.
    chat_choice_model: str = "deepseek-flash"  # 선택지 전용
    @property
    def effective_chat_judgement_model(self) -> str:
        """판정 호출에 쓸 모델. CHAT_JUDGEMENT_MODEL이 비면 CHAT_MODEL을 따른다(KNK-1620)."""
        return self.chat_judgement_model.strip() or self.chat_model

    # provider는 더 이상 설정값이 아니다(KNK-674). 위 모델 이름들을 등록부가 해석해
    # 호출별로 정한다(`llm.provider_of`) — 스토리와 채팅을 서로 다른 회사로 돌릴 수 있어야
    # 하는데, 전역값 하나로는 둘 중 하나가 반드시 거짓이 되기 때문이다.
    # 옛 LLM_PROVIDER env가 남아 있어도 무시된다(model_config의 extra="ignore").

    # 이미지 생성(KNK-938). 텍스트 LLM과 별도 모듈(src/services/image/).
    # API 키는 공급자별 기존 키를 재사용한다(OpenAI 이미지 → openai_api_key).
    image_model: str = "gpt-image-2.5-flare"  # 부모·자식·표지 이미지 공통
    image_quality: str = "low"  # 이미지 화질 (low / medium / high)
    image_size: str = "1024x768"  # 이미지 크기 (가로 4:3)
    image_timeout: float = 60.0  # 이미지 1장 생성 제한 시간(초)
    # 부모 이미지 다운로드는 관리하는 CDN만 허용한다(JSON 배열 환경변수).
    image_parent_allowed_hosts: list[str] = ["cdn.manyak.app", "dev-cdn.manyak.app"]
    # 백엔드가 presigned PUT을 발급하는 S3 호스트만 지정한다. 미설정 시 업로드 거부.
    image_upload_allowed_hosts: list[str] = []

    # 게시물 검수 이미지(KNK-1359). 백엔드가 준 공개 서빙 URL을 서버가 내려받아 모델에 넣는다.
    # 허용 호스트는 부모 이미지 다운로드와 같은 CDN 목록(`image_parent_allowed_hosts`)을 쓴다.
    # 장당 상한 32MiB는 DeepSeek Vision 가이드의 한도다 — 두 검수 모델 중 작은 값을 따른다.
    moderation_image_timeout: float = 30.0  # 게시물 한 건의 이미지 전체를 내려받는 제한 시간(초)
    moderation_image_max_bytes: int = 32 * 1024 * 1024
    moderation_model: str = "gpt-6-luna"
    moderation_fallback_model: str = "deepseek-flash"
    moderation_call_timeout: float = 60.0
    moderation_request_timeout: float = 150.0

    # Sentry 오류 수집 (KNK-262). DSN이 비면 비활성(no-op) — 로컬·CI는 끈다.
    # environment·표본율은 server(SENTRY_ENVIRONMENT/SENTRY_TRACES_SAMPLE_RATE) 규약을 미러링한다.
    sentry_dsn: str = ""
    sentry_environment: str = "local"
    sentry_traces_sample_rate: float = 0.0

    # Langfuse LLM 관측 (KNK-624). 키가 비면 비활성(no-op) — Sentry DSN 규약과 동일하게 맞춘다.
    # Sentry와 달리 프롬프트·응답 원문을 싣는다(AN-4-10 원문 비수집의 명시적 예외 — 6-analytics §6-7 개정이 짝).
    # host는 가입 리전마다 다르다(us: cloud / eu: eu.cloud / jp: jp.cloud) — 기본값 us를 .env로 덮는다.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # SDK 환경 변수 정책 유지. 변환 오류도 init_langfuse의 관측 실패 격리 안에서 처리합니다.
    langfuse_sample_rate: str = "1.0"


settings = Settings()
