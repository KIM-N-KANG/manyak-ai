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


# ── Anthropic 접속 정보 (KNK-675) ───────────────────────────────────────────
def test_anthropic_credentials_are_optional(monkeypatch) -> None:
    """Anthropic 키·주소는 필수가 아니다 — 아직 이 공급자를 쓰는 모델이 없다.

    DeepSeek 키처럼 필수로 만들면, 이 공급자를 안 쓰는 환경(로컬·CI·현재 운영 전부)이
    설정 하나 때문에 기동에 실패한다.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)

    s = Settings(_env_file=None)

    assert s.anthropic_api_key == ""
    # 주소는 빈 문자열이 아니라 None이다 — "SDK 기본 주소를 쓴다"는 뜻이고, 빈 문자열이면
    # 기동 검사의 주소 형식 검사에 걸려 이 공급자를 쓰는 순간 기동이 실패한다.
    assert s.anthropic_api_url is None


# ── 전역 provider 설정 삭제 (KNK-674) ───────────────────────────────────────
def test_settings_no_longer_carries_a_global_provider(monkeypatch) -> None:
    """회사 이름은 설정이 아니라 모델 이름이 정한다 — 옛 필드가 부활하면 출처가 둘이 된다.

    전역값 하나로는 "스토리는 A사, 채팅은 B사"를 표현할 수 없어 반드시 한쪽이 거짓이 된다.
    그래서 필드를 지웠는데, 지웠다는 사실을 지키는 것이 주석뿐이라 되살려도 아무도 몰랐다
    (KNK-674 리뷰 M4). 부활하면 여기서 막는다.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    assert not hasattr(Settings(_env_file=None), "llm_provider")
