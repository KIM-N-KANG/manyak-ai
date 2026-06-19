from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "AI Service"
    app_version: str = "0.1.0"
    debug: bool = False

    # gemini_api_key: str
    # gemini_model: str = "gemini-2.5-flash-lite"

    upstage_api_key: str
    upstage_api_url: str = "https://api.upstage.ai/v1"
    upstage_model: str = "solar-pro2"


settings = Settings()
