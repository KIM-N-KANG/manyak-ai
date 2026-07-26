"""모델 등록부 — 모델 이름을 공급자·어댑터·인자로 해석한다(KNK-670).

**모델 단위로 적는다.** 같은 회사라도 모델마다 받는 인자가 다르기 때문이다 — 예를 들어
Anthropic Sonnet 4.6은 temperature를 받지만 Sonnet 5는 400으로 거부한다. 회사 단위로 묶으면
그 차이를 담을 자리가 없다.

미등록 모델은 어느 회사로 보낼지 결정할 수 없다. 그래서 선택된 모델이 등록부에 없거나 그
모델의 공급자 키가 비어 있으면 **서버 기동에서 실패**시킨다(`validate_selected_models`).
선택되지 않은 공급자의 키 부재는 허용한다 — DeepSeek만 쓰는 환경이 다른 회사 키 없이 떠야 한다.
"""

from dataclasses import dataclass

from src.core.config import settings
from src.services.llm.base import (
    ADAPTER_OPENAI_SDK,
    PROVIDER_DEEPSEEK,
    LlmConfigError,
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
    ),
    # 스토리라인·채팅(STORYLINES_MODEL·CHAT_MODEL). 같은 비추론 정책이지만 따로 적는다 —
    # 첫 토큰 지연이 작아야 하는 경로라 나중에 이 모델만 조정할 여지를 남긴다(KNK-208).
    "deepseek-v4-flash": ResolvedModel(
        model="deepseek-v4-flash",
        provider=PROVIDER_DEEPSEEK,
        adapter=ADAPTER_OPENAI_SDK,
        use_thinking=False,
        supports_temperature=True,
    ),
}


@dataclass(frozen=True)
class ProviderCredentials:
    """공급자 접속 정보. api_key_env는 값이 비었을 때 무엇을 채우라고 알려줄 env 이름이다."""

    api_key: str
    base_url: str | None
    api_key_env: str


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
        )
    raise LlmConfigError(f"공급자 '{provider}'의 접속 정보 규칙이 없습니다.")


def selected_models() -> tuple[tuple[str, str], ...]:
    """용도별로 선택된 모델을 (env 이름, 모델 이름) 쌍으로 돌려준다.

    env 이름을 함께 들고 다니는 이유가 둘이다. 기동 실패 메시지가 "무엇을 고쳐야 하는지"를
    가리켜야 하고, 용도별 제약(예: 채팅은 Anthropic 불가 — KNK-675)의 판정 단위가 이 env다.
    """
    return (
        ("STORYLINES_MODEL", settings.storylines_model),
        ("STORY_COMPILE_MODEL", settings.story_compile_model),
        ("CHAT_MODEL", settings.chat_model),
    )


def validate_selected_models() -> None:
    """선택된 모델이 등록돼 있고 그 공급자 키가 채워졌는지 확인한다. 위반 시 LlmConfigError.

    서버 시작 시 한 번 부른다 — 잘못 적은 모델 이름이나 빈 키는 첫 사용자 요청(502)이 아니라
    기동에서 드러나야 한다.
    """
    for env_name, model in selected_models():
        try:
            resolved = resolve(model)
        except LlmConfigError as exc:
            raise LlmConfigError(f"{env_name}: {exc}") from exc
        creds = credentials(resolved.provider)
        if not creds.api_key.strip():
            raise LlmConfigError(
                f"{env_name}={model}은 공급자 '{resolved.provider}'를 쓰는데 "
                f"{creds.api_key_env}가 비어 있습니다."
            )
