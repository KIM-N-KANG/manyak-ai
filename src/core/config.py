from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": .env에 남은 옛/무관 키(예: 폐기된 DEEPSEEK_MODEL·DEEPSEEK_CHAT_MODEL)를
    # 기동 실패로 만들지 않는다. pydantic-settings는 dotenv 여분 키를 기본 금지(extra_forbidden)해
    # 필드 rename(KNK-595) 뒤 옛 .env로 앱이 안 뜨는 문제가 있었다 — 프로세스 env는 원래 무시하므로
    # dotenv도 같게 맞춘다.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Service"
    app_version: str = "0.1.0"
    debug: bool = False

    # gemini_api_key: str
    # gemini_model: str = "gemini-2.5-flash-lite"

    deepseek_api_key: str
    deepseek_api_url: str = "https://api.deepseek.com"
    # 모델은 용도별 3개 env var로 분리한다(KNK-595). 스토리라인·채팅은 지금은 같은 flash 기본이지만
    # 독립적으로 바꿀 수 있도록 필드를 나눴다. manyak-infra의 Compose env 이름도 같이 맞춘다.
    story_compile_model: str = "deepseek-v4-pro"  # 스토리 컴파일 전용(pro)
    storylines_model: str = "deepseek-v4-flash"  # 스토리라인 생성 전용(fast, KNK-215)
    chat_model: str = "deepseek-v4-flash"  # 채팅 턴·선택지·판정 공용(fast, KNK-215)
    # 로깅 메타 provider. 모델은 응답(response.model)에서 읽지만 provider는 응답에 없어
    # config가 유일한 출처다 — 공급자 교체 시 여기만 바꾸면 로그가 정확해진다(KNK-243).
    llm_provider: str = "deepseek"

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
