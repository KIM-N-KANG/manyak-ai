from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "AI Service"
    app_version: str = "0.1.0"
    debug: bool = False

    # gemini_api_key: str
    # gemini_model: str = "gemini-2.5-flash-lite"

    deepseek_api_key: str
    deepseek_api_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"  # 스토리 컴파일용(pro)
    # 채팅 턴·선택지·스토리라인 생성 공용 fast 모델(KNK-215). 이름의 chat은 역사적 표기다.
    # env var(DEEPSEEK_CHAT_MODEL)는 manyak-infra와 묶인 계약이라 필드 rename은 별도 과제.
    deepseek_chat_model: str = "deepseek-v4-flash"
    # 로깅 메타 provider. 모델은 응답(response.model)에서 읽지만 provider는 응답에 없어
    # config가 유일한 출처다 — 공급자 교체 시 여기만 바꾸면 로그가 정확해진다(KNK-243).
    llm_provider: str = "deepseek"

    # Sentry 오류 수집 (KNK-262). DSN이 비면 비활성(no-op) — 로컬·CI는 끈다.
    # environment·표본율은 server(SENTRY_ENVIRONMENT/SENTRY_TRACES_SAMPLE_RATE) 규약을 미러링한다.
    sentry_dsn: str = ""
    sentry_environment: str = "local"
    sentry_traces_sample_rate: float = 0.0


settings = Settings()
