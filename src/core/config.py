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
    deepseek_model: str = "deepseek-v4-pro"  # 스토리 컴파일용
    deepseek_chat_model: str = "deepseek-v4-flash"  # 채팅 턴용
    # 로깅 메타 provider. 모델은 응답(response.model)에서 읽지만 provider는 응답에 없어
    # config가 유일한 출처다 — 공급자 교체 시 여기만 바꾸면 로그가 정확해진다(KNK-243).
    llm_provider: str = "deepseek"


settings = Settings()
