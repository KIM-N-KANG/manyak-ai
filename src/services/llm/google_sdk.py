"""Google SDK 어댑터 — Gemini 모델 전용(KNK-951).

google-genai SDK로 Gemini API를 부른다. 하는 일은 OpenAI·Anthropic 어댑터와 같다.

1. 등록부의 **뜻**을 이 SDK의 **문법**으로 옮긴다.
2. 응답에서 본문·모델명·토큰을 꺼내 `LlmResult`로 만든다.
3. SDK 예외를 공급자 중립 예외로 접는다.
4. 단발 호출 1건을 Langfuse generation 관측으로 남긴다(KNK-1097). `langfuse.openai` 자동 계측은
   openai SDK만 덮어 google-genai 호출은 지나친다 — 이미지 어댑터(KNK-1240)처럼 여기서 손으로
   기록해야 Gemini 컴파일의 토큰·비용이 현재 트레이스(스토리 컴파일 등)에 모인다.
   스트리밍(`stream`)은 아직 기록하지 않는다. `observe_generation`은 현재 컨텍스트에 관측을
   붙이는 방식이라 yield를 넘나들면 중도 이탈이 ERROR로 남고 멈춘 사이 만든 관측의 부모가
   틀어진다. Gemini는 지금 컴파일(단발)에만 쓰이므로, 채팅에 쓰게 될 때 스트림 전용 관측과 함께 붙인다.
"""

import hashlib
import logging
from collections.abc import AsyncIterator

import httpx
from google import genai
from google.genai import errors, types

from src.core.langfuse import observe_generation
from src.services.llm.base import (
    STRUCTURED_OUTPUT_JSON_OBJECT,
    LlmBadRequest,
    LlmConfigError,
    message_has_images,
    LlmError,
    LlmRateLimited,
    LlmRequest,
    LlmResult,
    LlmTimeout,
    LlmUnavailable,
    ResolvedModel,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    TokenUsage,
)
from src.services.llm import registry

logger = logging.getLogger(__name__)

# 공급자별 클라이언트를 재사용한다(openai_sdk와 같은 이유).
_clients: dict[tuple[str, str | None, str], genai.Client] = {}

# 이 어댑터는 조각 흘리기를 한다. 기동 검사가 읽는다.
SUPPORTS_STREAMING = True

# Langfuse 관측 이름. 자동 계측이 붙이는 "OpenAI-generation"과 나란히 공급자를 드러낸다.
_OBSERVATION_NAME = "Gemini-generation"


def _fingerprint(api_key: str) -> str:
    """캐시 이름표용 키 지문(openai_sdk와 같은 이유)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _client(provider: str) -> genai.Client:
    """Google 클라이언트를 얻는다(없으면 만들어 캐시)."""
    creds = registry.credentials(provider)
    if not creds.api_key.strip():
        raise LlmConfigError(
            f"공급자 '{provider}' 호출에 필요한 {creds.api_key_env}가 비어 있습니다."
        )
    key = (provider, creds.base_url, _fingerprint(creds.api_key))
    client = _clients.get(key)
    if client is None:
        client = genai.Client(api_key=creds.api_key)
        _clients[key] = client
    return client


def check_supported(resolved: ResolvedModel) -> None:
    """이 어댑터가 이 모델의 설정을 요청 인자로 표현할 수 있는지 확인한다."""
    _build_config(
        LlmRequest(model=resolved.model, messages=[{"role": "user", "content": "test"}]),
        resolved,
    )


def _build_config(req: LlmRequest, resolved: ResolvedModel) -> types.GenerateContentConfig:
    """SDK에 넘길 설정을 조립한다."""
    if message_has_images(req.messages):
        # 이미지 조각을 이 회사 문법(`types.Part.from_bytes`)으로 아직 옮기지 않는다(KNK-1359는
        # OpenAI SDK 어댑터만). 그대로 보내면 `_build_contents`가 조각 목록을 글로 취급해
        # 이미지가 조용히 사라지므로 설정 오류로 막는다 — anthropic_sdk와 같은 규칙.
        raise LlmConfigError(
            f"모델 '{resolved.model}'의 어댑터는 이미지 조각을 아직 옮기지 못합니다."
        )
    config_kwargs: dict[str, object] = {}

    # system instruction — messages에서 system role을 분리한다.
    # Gemini의 system_instruction은 칸이 하나뿐이라 복수 system 메시지를 합친다.
    # **채팅처럼 system을 앞뒤에 나눠 놓는 경우(앞 1 + 뒤 2) 뒤쪽 지시의 배치 효과가
    # 사라진다** — Anthropic과 같은 문제다. 컴파일은 system이 앞 1개뿐이라 지금은 문제없지만,
    # 채팅에서 이 어댑터를 쓰려면 BLOCKED_PROVIDERS에 넣거나 배치 문제를 먼저 풀어야 한다.
    system_parts = []
    for msg in req.messages:
        if msg.get("role") == "system":
            system_parts.append(msg.get("content", ""))
    if system_parts:
        config_kwargs["system_instruction"] = "\n\n".join(system_parts)

    # temperature
    if req.temperature is not None:
        if resolved.supports_temperature:
            config_kwargs["temperature"] = req.temperature
        else:
            logger.info(
                "모델 %s는 temperature를 받지 않아 인자를 뺐다(요청값 %s)",
                resolved.model,
                req.temperature,
            )

    # max_output_tokens
    if req.max_tokens is not None:
        if resolved.max_output_tokens is not None and req.max_tokens > resolved.max_output_tokens:
            raise LlmConfigError(
                f"모델 '{resolved.model}'의 max_tokens={req.max_tokens}이 최대 출력 "
                f"{resolved.max_output_tokens}을 넘습니다."
            )
        config_kwargs["max_output_tokens"] = req.max_tokens

    # JSON mode
    if req.json_mode:
        if (
            resolved.structured_output_modes
            and STRUCTURED_OUTPUT_JSON_OBJECT not in resolved.structured_output_modes
        ):
            raise LlmConfigError(
                f"모델 '{resolved.model}'은 json_object 구조화 출력을 지원하지 않습니다: "
                f"{sorted(resolved.structured_output_modes)}"
            )
        config_kwargs["response_mime_type"] = "application/json"

    # thinking config (reasoning effort)
    if resolved.use_thinking and resolved.reasoning_effort is not None:
        if (
            resolved.supported_reasoning_efforts
            and resolved.reasoning_effort not in resolved.supported_reasoning_efforts
        ):
            raise LlmConfigError(
                f"모델 '{resolved.model}'의 추론 강도 '{resolved.reasoning_effort}'가 지원 목록에 "
                f"없습니다: {sorted(resolved.supported_reasoning_efforts)}"
            )
        # Gemini 3.x는 thinking_level로 문자열(low/medium/high)을 그대로 받는다.
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=resolved.reasoning_effort
        )

    # 타임아웃 — Google SDK는 GenerateContentConfig가 아니라 http_options로 넘긴다.
    if req.timeout is not None:
        config_kwargs["http_options"] = types.HttpOptions(timeout=int(req.timeout * 1000))

    return types.GenerateContentConfig(**config_kwargs)


def _build_contents(req: LlmRequest) -> list[types.Content]:
    """messages를 Gemini contents 형식으로 변환한다.

    system role은 config.system_instruction으로 분리했으므로 여기서는 제외한다.
    Gemini는 role이 "user"와 "model"(assistant) 둘이다.
    """
    contents: list[types.Content] = []
    for msg in req.messages:
        role = msg.get("role", "user")
        if role == "system":
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part(text=msg.get("content", ""))],
            )
        )
    return contents


def _text_of(response: object) -> str:
    """본문을 꺼낸다. 못 꺼내면 빈 문자열(openai_sdk와 같은 규칙)."""
    try:
        return response.text  # type: ignore[attr-defined]
    except Exception:
        logger.warning("응답에서 본문을 꺼내지 못했다 — 빈 본문으로 넘긴다", exc_info=True)
        return ""


def _usage_of(response: object) -> TokenUsage:
    """usage를 옮긴다. thinking 토큰은 output에 합산한다(비용·지표 정합성)."""
    usage = getattr(response, "usage_metadata", None)
    candidates = getattr(usage, "candidates_token_count", None)
    thoughts = getattr(usage, "thoughts_token_count", None)
    if candidates is not None and thoughts is not None:
        output_tokens = candidates + thoughts
    elif candidates is not None:
        output_tokens = candidates
    else:
        output_tokens = thoughts  # 둘 다 None이면 None
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_token_count", None),
        output_tokens=output_tokens,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=getattr(usage, "cached_content_token_count", None),
    )


def _usage_details(response: object) -> dict[str, int] | None:
    """usage_metadata를 Langfuse usage_details로 옮긴다.

    키는 Langfuse 관리 단가(gemini-3.x-flash)가 값을 매긴 이름에 맞춘다 — 이름이 어긋나면 그
    토큰은 0원으로 계산되고, 겹치면 두 번 계산된다. 그래서:

    - prompt_token_count는 캐시 적중분을 포함하므로 캐시를 뺀 값은 input, 적중분은
      input_cached_tokens로 나눈다(캐시 읽기 단가는 1/10).
    - thinking 토큰은 output_reasoning으로 따로 싣고 output에는 합치지 않는다. 관리 단가가
      output_reasoning에도 출력 단가를 매기므로, 합친 output과 output_reasoning을 함께 보내면
      thinking이 두 번 과금된다. (`_usage_of`가 output에 합산하는 것은 내부 지표용 TokenUsage
      규칙이라 여기와 다르다.)
    - total은 공급자 합계(prompt + candidates + thoughts)를 그대로 싣는다.
    - tool 호출은 쓰지 않아 tool_use_prompt_token_count는 옮기지 않는다. tools를 도입하면
      그 입력 토큰도 input에 더해야 비용이 빠지지 않는다.

    값이 하나도 없으면 None을 돌려준다.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_token_count", None)
    cached = getattr(usage, "cached_content_token_count", None)
    candidates = getattr(usage, "candidates_token_count", None)
    thoughts = getattr(usage, "thoughts_token_count", None)
    details: dict[str, int] = {}
    if isinstance(prompt, int):
        cached_count = cached if isinstance(cached, int) else 0
        # 정상 응답은 cached ≤ prompt지만, 어긋난 값이 음수 비용으로 번지지 않게 막는다.
        details["input"] = max(0, prompt - cached_count)
        if cached_count:
            details["input_cached_tokens"] = cached_count
    if isinstance(candidates, int):
        details["output"] = candidates
    if isinstance(thoughts, int):
        details["output_reasoning"] = thoughts
    if not details:
        return None
    details["total"] = sum(v for v in (prompt, candidates, thoughts) if isinstance(v, int))
    return details


def _model_parameters(
    req: LlmRequest, resolved: ResolvedModel
) -> dict[str, str | int | float | bool | None]:
    """관측에 실을 모델 인자. 실제 전송 여부와 무관하게 요청·등록부가 정한 값만 요약한다."""
    params: dict[str, str | int | float | bool | None] = {}
    if req.temperature is not None and resolved.supports_temperature:
        params["temperature"] = req.temperature
    if req.max_tokens is not None:
        params["max_tokens"] = req.max_tokens
    if resolved.use_thinking and resolved.reasoning_effort is not None:
        params["reasoning_effort"] = resolved.reasoning_effort
    if req.json_mode:
        params["json_mode"] = True
    return params


def _finish_reason_of(response: object) -> str | None:
    """종료 이유를 꺼낸다. 못 꺼내면 None.

    Gemini는 enum(FinishReason.STOP)으로 주므로 .name을 소문자로 바꿔 OpenAI 계열("stop")과
    형식을 맞춘다.
    """
    try:
        candidates = response.candidates  # type: ignore[attr-defined]
        if candidates:
            reason = candidates[0].finish_reason
            if reason is not None:
                return reason.name.lower() if hasattr(reason, "name") else str(reason)
    except Exception:
        logger.warning("응답에서 종료 이유를 꺼내지 못했다 — None으로 넘긴다", exc_info=True)
    return None


def _translate(exc: Exception, resolved: ResolvedModel) -> LlmError:
    """SDK 예외를 공급자 중립 예외로 접는다."""
    context = {"provider": resolved.provider, "model": resolved.model}
    msg = str(exc)
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        if code == 408 or "timeout" in msg.lower():
            return LlmTimeout(msg, **context)
        if code == 429:
            return LlmRateLimited(msg, **context)
        if code == 400:
            return LlmBadRequest(msg, **context)
        return LlmUnavailable(msg, **context)
    # SDK 밖으로 새는 전송 오류 — httpx 타임아웃은 타임아웃으로 분류한다.
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return LlmTimeout(f"{type(exc).__name__}: {msg}", **context)
    return LlmUnavailable(f"{type(exc).__name__}: {msg}", **context)


async def complete(req: LlmRequest, resolved: ResolvedModel) -> LlmResult:
    """단발 호출."""
    client = _client(resolved.provider)
    config = _build_config(req, resolved)
    contents = _build_contents(req)
    # 관측 블록 안에서 나가는 예외(공급자 실패)는 관측에 ERROR로 남고 그대로 전파된다.
    with observe_generation(
        _OBSERVATION_NAME,
        model=resolved.model,
        model_parameters=_model_parameters(req, resolved),
        input_data=req.messages,
    ) as generation:
        try:
            response = await client.aio.models.generate_content(
                model=resolved.model,
                contents=contents,
                config=config,
            )
        except errors.APIError as exc:
            raise _translate(exc, resolved) from exc
        except (httpx.TimeoutException, httpx.TransportError, ConnectionError, TimeoutError, OSError) as exc:
            raise _translate(exc, resolved) from exc
        text = _text_of(response)
        generation.finish(output=text, usage_details=_usage_details(response))
    return LlmResult(
        text=text,
        model=getattr(response, "model_version", None) or req.model,
        provider=resolved.provider,
        usage=_usage_of(response),
        finish_reason=_finish_reason_of(response),
    )


async def stream(req: LlmRequest, resolved: ResolvedModel) -> AsyncIterator[StreamEvent]:
    """스트리밍 호출. 조각을 TextDelta로 흘리고 마지막에 StreamCompleted를 낸다."""
    client = _client(resolved.provider)
    config = _build_config(req, resolved)
    contents = _build_contents(req)
    model = req.model
    usage = TokenUsage()
    finish_reason: str | None = None
    try:
        async for chunk in await client.aio.models.generate_content_stream(
            model=resolved.model,
            contents=contents,
            config=config,
        ):
            # usage 수집
            chunk_usage = getattr(chunk, "usage_metadata", None)
            if chunk_usage is not None:
                usage = _usage_of(chunk)
            # model version 수집
            model_version = getattr(chunk, "model_version", None)
            if model_version:
                model = model_version
            # finish_reason 수집
            candidates = getattr(chunk, "candidates", None)
            if candidates:
                reason = getattr(candidates[0], "finish_reason", None)
                if reason is not None:
                    finish_reason = reason.name.lower() if hasattr(reason, "name") else str(reason)
            # 텍스트 조각 흘리기
            try:
                text = chunk.text
            except Exception:
                text = None
            if isinstance(text, str) and text:
                yield TextDelta(text)
    except errors.APIError as exc:
        raise _translate(exc, resolved) from exc
    except (httpx.TimeoutException, httpx.TransportError, ConnectionError, TimeoutError, OSError) as exc:
        raise _translate(exc, resolved) from exc
    yield StreamCompleted(
        model=model,
        provider=resolved.provider,
        usage=usage,
        finish_reason=finish_reason,
    )
