"""API 응답 로깅 메타(백엔드 `ai_call_logs` 적재용) — KNK-243.

3개 엔드포인트(storylines·compile·chat completed) 응답에 nested `meta`로 실린다.
와이어 표기는 각 응답의 기존 관례를 따른다: story=snake_case, chat=camelCase.
값 출처는 호출 경계 한 곳에서 묶는다 — `model`은 실제 응답(response.model),
`prompt_versions`는 frontmatter(KNK-229 상수), `provider`는 config, 토큰은 LLM usage.
"""

from pydantic import BaseModel, ConfigDict, Field


class StoryResponseMeta(BaseModel):
    """storylines·compile 응답 메타(snake_case). `model` 필드명은 보호 네임스페이스라 해제."""

    model_config = ConfigDict(protected_namespaces=())

    model: str  # 단일어라 snake/camel 동일 — alias 불필요
    prompt_versions: dict[str, int]
    provider: str
    input_token_count: int | None
    output_token_count: int | None
    retry_count: int


class ChatResponseMeta(BaseModel):
    """chat completed 이벤트 메타(camelCase). model_dump(by_alias=True)로 직렬화한다."""

    model_config = ConfigDict(protected_namespaces=())

    model: str  # 단일어라 snake/camel 동일 — alias 불필요
    prompt_versions: dict[str, int] = Field(serialization_alias="promptVersions")
    provider: str
    input_token_count: int | None = Field(serialization_alias="inputTokenCount")
    output_token_count: int | None = Field(serialization_alias="outputTokenCount")
    retry_count: int = Field(serialization_alias="retryCount")
