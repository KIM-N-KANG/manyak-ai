from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": .env에 남은 옛/무관 키(예: 폐기된 DEEPSEEK_MODEL·DEEPSEEK_CHAT_MODEL)를
    # 기동 실패로 만들지 않는다. pydantic-settings는 dotenv 여분 키를 기본 금지(extra_forbidden)해
    # 필드 rename(KNK-595) 뒤 옛 .env로 앱이 안 뜨는 문제가 있었다 — 프로세스 env는 원래 무시하므로
    # dotenv도 같게 맞춘다.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Service"
    app_version: str = "0.3.1"
    debug: bool = False

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
    # 모델은 용도별 3개 env var로 분리한다(KNK-595). 스토리라인·채팅은 지금은 같은 flash 기본이지만
    # 독립적으로 바꿀 수 있도록 필드를 나눴다. manyak-infra의 Compose env 이름도 같이 맞춘다.
    story_compile_model: str = "gpt-5.6-terra"  # 스토리 컴파일 전용
    storylines_model: str = "deepseek-v4-flash"  # 스토리라인 생성 전용(fast, KNK-215)
    chat_model: str = "deepseek-v4-flash"  # 채팅 턴·선택지·판정 공용(fast, KNK-215)
    # provider는 더 이상 설정값이 아니다(KNK-674). 위 세 모델 이름을 등록부가 해석해
    # 호출별로 정한다(`llm.provider_of`) — 스토리와 채팅을 서로 다른 회사로 돌릴 수 있어야
    # 하는데, 전역값 하나로는 둘 중 하나가 반드시 거짓이 되기 때문이다.
    # 옛 LLM_PROVIDER env가 남아 있어도 무시된다(model_config의 extra="ignore").

    # 이미지 생성(KNK-938). 텍스트 LLM과 별도 모듈(src/services/image/).
    # API 키는 공급자별 기존 키를 재사용한다(gpt-image-2 → openai_api_key).
    image_model: str = "gpt-image-2-2026-04-21"  # 컴파일 인물 이미지 전용 (스냅샷 고정)
    image_quality: str = "low"  # 이미지 화질 (low / medium / high)
    image_size: str = "1024x768"  # 이미지 크기 (가로 4:3)
    image_timeout: float = 60.0  # 이미지 1장 생성 제한 시간(초)

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


settings = Settings()
