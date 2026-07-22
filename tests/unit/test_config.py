from src.core.config import Settings


# ── 모델 env var 3분리 (KNK-595) ─────────────────────────────────────────────
def test_model_fields_read_new_env_names(monkeypatch) -> None:
    """새 env 이름(STORY_COMPILE_MODEL·STORYLINES_MODEL·CHAT_MODEL)이 각 필드로 매핑된다."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("STORY_COMPILE_MODEL", "compile-x")
    monkeypatch.setenv("STORYLINES_MODEL", "storylines-x")
    monkeypatch.setenv("CHAT_MODEL", "chat-x")

    s = Settings(_env_file=None)

    assert s.story_compile_model == "compile-x"
    assert s.storylines_model == "storylines-x"
    assert s.chat_model == "chat-x"


def test_model_defaults_unchanged(monkeypatch) -> None:
    """env 미설정 시 기본 모델은 rename 전과 동일하다(순수 이름 분리, 동작 무변경)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    s = Settings(_env_file=None)

    assert s.story_compile_model == "deepseek-v4-pro"
    assert s.storylines_model == "deepseek-v4-flash"
    assert s.chat_model == "deepseek-v4-flash"


def test_legacy_env_file_keys_do_not_break_startup(tmp_path, monkeypatch) -> None:
    """옛 .env(폐기된 DEEPSEEK_MODEL·DEEPSEEK_CHAT_MODEL 포함)로도 기동해야 한다(KNK-595 회귀).

    필드 rename 뒤 pydantic-settings의 dotenv 여분 키 기본 금지(extra_forbidden) 때문에
    옛 .env로 앱이 안 뜨던 회귀를 고정한다(extra="ignore"). Codex 리뷰 P2 지적 반영.
    """
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHAT_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "DEEPSEEK_API_KEY=test-key\n"
        "DEEPSEEK_MODEL=deepseek-v4-pro\n"
        "DEEPSEEK_CHAT_MODEL=deepseek-v4-flash\n",
        encoding="utf-8",
    )

    s = Settings(_env_file=str(env))  # 예외 없이 로드돼야 한다

    # 옛 키는 무시되고 기본값이 유지된다
    assert s.story_compile_model == "deepseek-v4-pro"
    assert s.chat_model == "deepseek-v4-flash"
