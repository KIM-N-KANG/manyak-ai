"""LLM 공통 통로 — 모델 다양성에 유연한 호출 구조(KNK-667).

호출부는 "무엇을 원하는지"만 넘기고, 모델별 차이(어느 회사 SDK인지·어떤 인자를 받는지)는
아래 층이 흡수한다. 그래서 모델을 바꿀 때 호출부를 고치지 않는다.

- `base.py`       — 요청·결과·스트림 이벤트·공급자 중립 예외 (층 사이의 공용 타입)
- `registry.py`   — 모델 등록부. 모델 이름 → 공급자·어댑터·호출 특성 (뜻만 적는다)
- `openai_sdk.py` — OpenAI SDK 어댑터(DeepSeek·GPT 공용). 뜻을 회사 문법으로 옮긴다
- `anthropic_sdk.py` — Anthropic SDK 어댑터. KNK-675에서 추가한다

호출용 공개 함수는 `complete`·`stream` 둘이다. **둘 다 단발 호출이다** — 재호출·시간 예산·
검증은 호출부가 관장한다(스토리라인 invalid 재호출 KNK-312이 통로로 올라오면 이관 범위가
폭발한다). 여기에 기동 검사용 `validate_startup`이 더해져 공개 함수는 셋이다.
"""

from collections.abc import AsyncIterator

from src.services.llm import registry
from src.services.llm.base import (
    ADAPTER_OPENAI_SDK,
    LlmAdapter,
    LlmConfigError,
    LlmRequest,
    LlmResult,
    ResolvedModel,
    StreamEvent,
)

__all__ = ["complete", "stream", "validate_startup"]


def validate_startup() -> None:
    """기동 시 한 번 부른다 — 설정만으로 알 수 있는 문제를 첫 요청 전에 드러낸다.

    등록·키 검사(등록부)에 더해, **담당 어댑터가 그 모델의 설정을 실제로 표현할 수 있는지**까지
    본다. 이걸 빼면 어댑터가 모르는 공급자 문법이 첫 사용자 요청에서야 LlmConfigError로 터지는데,
    그 예외는 LlmError가 아니라서 실패를 흡수해야 하는 경로(선택지 폴백·판정 null)를 관통해
    500이 된다.

    여기서 막으면 **배포가 실패하고 AI 서버가 내려간다**(배포는 기존 컨테이너를 교체한다 —
    `docker compose up -d --wait ai`). 그래도 이쪽이 낫다: 잘못된 설정으로 뜬 서버는 사용자
    요청마다 500·502를 내면서도 살아 있는 것처럼 보여, 문제를 훨씬 늦게 알게 된다.

    어댑터 선택 실패(`_adapter_of`)도 함께 잡아 어느 env가 문제인지 붙인다 — 메시지만 보고
    STORYLINES_MODEL·STORY_COMPILE_MODEL·CHAT_MODEL 중 무엇을 고칠지 알 수 있어야 한다.
    """
    registry.validate_selected_models()
    for env_name, model in registry.selected_models():
        try:
            adapter, resolved = _adapter_of(model)
            adapter.check_supported(resolved)
        except LlmConfigError as exc:
            raise LlmConfigError(f"{env_name}: {exc}") from exc


def _adapter_of(model: str) -> tuple[LlmAdapter, ResolvedModel]:
    """모델 이름을 해석하고 담당 어댑터 모듈을 고른다.

    등록부에 없는 모델은 여기서 걸러진다(기동 검사가 놓치는 런타임 모델 교체 대비).

    어댑터는 **모듈 안에서 늦게 가져온다.** 이 모듈 맨 위에서 가져오면 `src.services.llm`을
    건드리는 모든 곳이 어댑터까지 함께 불러오게 되고, 어댑터가 `src.core.sentry`를 쓰는 순간
    (다음 단계 KNK-674) sentry → llm.base → llm → 어댑터 → sentry로 순환 import가 된다.
    """
    resolved = registry.resolve(model)
    if resolved.adapter == ADAPTER_OPENAI_SDK:
        from src.services.llm import openai_sdk

        return openai_sdk, resolved
    raise LlmConfigError(
        f"모델 '{resolved.model}'의 어댑터 '{resolved.adapter}'를 처리할 코드가 없습니다."
    )


async def complete(req: LlmRequest) -> LlmResult:
    """LLM을 한 번 부르고 결과를 돌려준다(스토리라인·컴파일·선택지·판정)."""
    adapter, resolved = _adapter_of(req.model)
    return await adapter.complete(req, resolved)


def stream(req: LlmRequest) -> AsyncIterator[StreamEvent]:
    """LLM 응답을 조각으로 흘린다(채팅 본문).

    async generator를 그대로 돌려준다 — 모델 해석 실패는 첫 조각을 기다리기 전에,
    호출한 자리에서 바로 드러나야 한다.
    """
    adapter, resolved = _adapter_of(req.model)
    return adapter.stream(req, resolved)
