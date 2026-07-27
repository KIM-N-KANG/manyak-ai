"""OpenAI SDK 어댑터 — DeepSeek·GPT 공용(KNK-671).

어댑터는 **SDK 계열 단위**다. DeepSeek과 GPT는 같은 OpenAI SDK로 부르고 주소·키만 다르므로
이 파일 하나가 둘을 담당한다. 하는 일은 셋이다.

1. 등록부의 **뜻**(추론 모드·temperature 수용)을 이 SDK의 **문법**으로 옮긴다.
2. 응답에서 본문·모델명·토큰을 꺼내 `LlmResult`로 만든다. 응답 껍데기가 깨져도 예외를
   던지지 않는다 — 쓸 수 있는 응답인지 판정하는 일은 호출부 몫이다.
3. SDK 예외를 공급자 중립 예외로 접는다(순서 주의 — 아래 `_translate`).
"""

import hashlib
import json
import logging
from collections.abc import AsyncIterator

import httpx
from openai import (
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    OpenAIError,
    RateLimitError,
)

from src.services.llm.base import (
    PROVIDER_DEEPSEEK,
    LlmBadRequest,
    LlmConfigError,
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
# 모듈째 가져온다(`from ... import credentials` 아님). 이름을 직접 묶어두면 테스트가
# `registry.credentials`를 갈아끼워도 어댑터에는 반영되지 않아, 가짜를 세운 줄 알고 실제 주소로
# 호출이 나갈 수 있다.
from src.services.llm import registry

logger = logging.getLogger(__name__)

# 공급자별 클라이언트를 재사용한다 — 호출마다 새로 만들면 커넥션 풀·TLS 세션이 매번 버려져
# 호출당 핸드셰이크가 붙는다. 캐시 이름표에는 주소와 **API 키의 지문**을 넣어, 설정이 런타임에
# 바뀌면(테스트·실험) 새 클라이언트가 만들어지게 한다.
#
# 설정이 바뀔 때 **옛 클라이언트를 닫지 않고 버린다**(축출 없음) — 그쪽이 쥔 커넥션과 옛 키가
# 프로세스 끝까지 남는다. 운영은 설정이 고정이라 클라이언트가 1개뿐이라 영향이 없고, 키를
# 바꿔가며 도는 실험·스크립트에서만 쌓인다. **프로덕션 안정화 후로 미룬 사안**(사용자 결정,
# KNK-671 리뷰) — 고칠 때는 이름표가 바뀌면 옛 것을 await로 닫고 지우면 된다.
_clients: dict[tuple[str, str | None, str], AsyncOpenAI] = {}


def _fingerprint(api_key: str) -> str:
    """캐시 이름표에 넣을 키 지문.

    키 원문을 이름표로 쓰면 안 된다. 이름표는 예외 메시지·디버그 로그·오류 보고에 통째로
    딸려 나갈 수 있는 값이라, 원문을 넣으면 **API 키가 그런 경로로 새어 나간다.** 지문은
    "설정이 바뀌었는지" 판정에 충분하다.

    (Sentry의 지역변수 수집은 별도로 껐다 — `src.core.sentry.init_sentry`의
    `include_local_variables=False`. 그쪽 한 곳만 믿지 않고 값 자체를 안 남기는 쪽도 함께 둔다.)
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _client(provider: str) -> AsyncOpenAI:
    """공급자의 클라이언트를 얻는다(없으면 만들어 캐시).

    키가 비어 있으면 여기서 `LlmConfigError`로 막는다. 그냥 두면 SDK 생성자가 자기 예외
    (`OpenAIError`)를 던지는데, 그 예외는 호출부가 아는 중립 예외가 아니라 번역 없이 새어 나간다.
    성질상으로도 전송 실패가 아니라 설정 오류다. 공백만 있는 키는 SDK가 통과시키므로
    (`api_key=" "`) 여기서 함께 막아 기동 검사와 판정을 일치시킨다.

    timeout·max_retries를 클라이언트에 박지 않는다. 타임아웃은 호출마다 다르고
    (`LlmRequest.timeout`), 재시도는 SDK 기본값(2회)을 그대로 둬 지금 동작을 보존한다 —
    호출부의 "전송 실패는 SDK 재시도가 맡는다"는 전제(`story_llm._complete_json`)가 이 값이다.
    """
    creds = registry.credentials(provider)
    if not creds.api_key.strip():
        raise LlmConfigError(
            f"공급자 '{provider}' 호출에 필요한 {creds.api_key_env}가 비어 있습니다."
        )
    key = (provider, creds.base_url, _fingerprint(creds.api_key))
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=creds.api_key, base_url=creds.base_url)
        _clients[key] = client
    return client


def _provider_kwargs(resolved: ResolvedModel) -> dict[str, object]:
    """등록부의 뜻을 이 SDK 계열의 문법으로 옮긴다 — OpenAI 계열은 정식 인자가 아니라 extra_body로 싣는다.

    매번 새 dict를 만든다. 등록부 값을 그대로 넘겨 어댑터가 제자리에서 고치면 그 모델의 설정이
    프로세스 내내 오염되기 때문이다.

    문법을 모르는 공급자에서 "추론 끄기"를 지시받으면 **거부한다.** 조용히 넘기면 등록부에
    끄라고 적어둔 모델이 추론이 켜진 채 호출된다 — 등록부가 추론 모드를 반드시 정하게 만든
    취지(`base.ResolvedModel.use_thinking`에 기본값이 없는 이유)가 여기서 무너진다.
    """
    if resolved.provider == PROVIDER_DEEPSEEK:
        # DeepSeek V4는 추론이 기본이라 끌 때만 인자를 싣는다 — 창작 태스크에서 비추론이 더
        # 안정적이었다(KNK-208 벤치).
        return {} if resolved.use_thinking else {"thinking": {"type": "disabled"}}
    if not resolved.use_thinking:
        raise LlmConfigError(
            f"공급자 '{resolved.provider}'에서 추론을 끄는 문법을 이 어댑터가 모릅니다 — "
            f"모델 '{resolved.model}'을 쓰려면 어댑터에 그 공급자의 문법을 먼저 추가하세요."
        )
    return {}


def check_supported(resolved: ResolvedModel) -> None:
    """이 어댑터가 이 모델의 설정을 요청 인자로 표현할 수 있는지 확인한다. 못 하면 LlmConfigError.

    기동 검사가 부른다(`llm.validate_startup`). 실제 호출은 하지 않으므로 과금도 지연도 없다 —
    인자를 조립해보고 버린다. 이걸 기동에서 안 보면 첫 사용자 요청에서 터진다.
    """
    _provider_kwargs(resolved)


def _build_kwargs(req: LlmRequest, resolved: ResolvedModel) -> dict[str, object]:
    """SDK에 넘길 인자를 조립한다. 값이 없는 인자는 **아예 넣지 않는다**(SDK 기본값에 맡긴다)."""
    kwargs: dict[str, object] = {"model": resolved.model, "messages": req.messages}
    if req.json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if req.temperature is not None:
        if resolved.supports_temperature:
            kwargs["temperature"] = req.temperature
        else:
            # 안 받는 모델에 보내면 400이다. 몰래 다른 값으로 바꾸지 않고 인자를 뺀다 —
            # 뺐다는 사실은 남겨, 온도 설정이 조용히 무시된 것을 나중에 알 수 있게 한다.
            logger.info(
                "모델 %s는 temperature를 받지 않아 인자를 뺐다(요청값 %s)",
                resolved.model,
                req.temperature,
            )
    if req.max_tokens is not None:
        kwargs["max_tokens"] = req.max_tokens
    if req.timeout is not None:
        kwargs["timeout"] = req.timeout
    provider_kwargs = _provider_kwargs(resolved)
    if provider_kwargs:
        kwargs["extra_body"] = provider_kwargs
    return kwargs


def _text_of(response: object) -> str:
    """본문을 꺼낸다. **어떤 모양이 와도 예외를 던지지 않는다** — 못 꺼내면 빈 문자열이다.

    이상한 응답 모양을 하나씩 막지 않고 규칙 하나로 둔다. 빈 choices·message 없음뿐 아니라
    목록이 아닌 choices, 글자가 아닌 content까지 경우가 끝이 없어, 나열하면 새 모양이 나올
    때마다 코드가 늘어난다.

    붙잡은 것을 공급자 장애로 둔갑시키지 않는다는 점이 중요하다 — "내용이 비었다"로 넘기면
    호출부가 판정해 스토리라인 invalid 재호출(KNK-312, 최대 2회)을 그대로 태운다. 여기서
    예외를 던지면 그 재호출이 전송 오류 경로로 새서 사라진다.

    붙잡을 때 **로그를 남긴다.** 안 남기면 "LLM이 빈 글을 줬다"와 "응답 모양이 이상해 못
    꺼냈다"가 호출부에서 똑같이 보여, 재호출이 왜 돌았는지 나중에 구분할 수 없다.
    """
    try:
        content = response.choices[0].message.content  # type: ignore[attr-defined]
    except Exception:  # 계약이 "절대 던지지 않는다"이므로 꺼내기 실패는 전부 빈 본문으로 본다.
        logger.warning("응답에서 본문을 꺼내지 못했다 — 빈 본문으로 넘긴다", exc_info=True)
        return ""
    return content if isinstance(content, str) else ""


def _usage_of(payload: object) -> TokenUsage:
    """usage를 옮긴다.

    `prompt_tokens`는 캐시 적중분을 **이미 포함한 합계**다(DeepSeek·GPT) — 여기서 캐시 값을
    더하면 두 번 세어 적재값이 부풀려진다. 캐시 적중 토큰은 내역으로만 남긴다.
    누락된 값은 0이 아니라 None으로 남긴다(백엔드 계약: 누락 시 null).
    """
    usage = getattr(payload, "usage", None)
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        # DeepSeek 전용 진단 필드. GPT는 다른 이름이라 등록 시(다음 단계) 함께 확인한다.
        cache_read_input_tokens=getattr(usage, "prompt_cache_hit_tokens", None),
    )


def _finish_reason_of(payload: object) -> str | None:
    """응답 종료 이유(잘림 감지용). `_text_of`와 같은 규칙 — 못 꺼내면 None이고 예외는 없다."""
    try:
        reason = payload.choices[0].finish_reason  # type: ignore[attr-defined]
    except Exception:
        logger.warning("응답에서 종료 이유를 꺼내지 못했다 — None으로 넘긴다", exc_info=True)
        return None
    return reason if isinstance(reason, str) else None


def _translate(exc: OpenAIError, resolved: ResolvedModel) -> LlmError:
    """SDK 예외를 공급자 중립 예외로 접는다.

    **순서가 중요하다.** `APITimeoutError`는 `APIConnectionError`의 하위 클래스라, 연결 오류를
    먼저 검사하면 모든 타임아웃이 unavailable로 분류돼 error_code와 사용자 문구까지 바뀐다.

    마지막은 catch-all이다 — 번역되지 않은 SDK 예외가 호출부까지 올라가면 실패를 흡수해야 하는
    경로(선택지 폴백·판정 null)가 흡수에 실패해 턴이 500으로 깨진다. 인증·권한·404·422·5xx는
    4xx 여부와 무관하게 여기로 묶인다(지금 `classify_error_code`의 분류를 그대로 보존).
    """
    context = {"provider": resolved.provider, "model": resolved.model}
    if isinstance(exc, APITimeoutError):
        return LlmTimeout(str(exc), **context)
    if isinstance(exc, RateLimitError):
        return LlmRateLimited(str(exc), **context)
    if isinstance(exc, BadRequestError):
        return LlmBadRequest(str(exc), **context)
    return LlmUnavailable(str(exc), **context)


async def complete(req: LlmRequest, resolved: ResolvedModel) -> LlmResult:
    """단발 호출. 재호출·시간 예산은 호출부가 관장하고 여기서는 한 번만 부른다."""
    client = _client(resolved.provider)
    try:
        response = await client.chat.completions.create(**_build_kwargs(req, resolved))
    except OpenAIError as exc:
        raise _translate(exc, resolved) from exc
    return LlmResult(
        text=_text_of(response),
        # 응답이 돌려준 실제 모델명. 비어 오면 요청에 쓴 이름으로 채운다 — 빈 값을 올리면
        # 응답 meta 조립에서 터진다.
        model=getattr(response, "model", None) or req.model,
        provider=resolved.provider,
        usage=_usage_of(response),
        finish_reason=_finish_reason_of(response),
    )


async def stream(req: LlmRequest, resolved: ResolvedModel) -> AsyncIterator[StreamEvent]:
    """스트리밍 호출. 조각을 TextDelta로 흘리고 마지막에 StreamCompleted를 낸다.

    사용자 연결 취소(`CancelledError`)는 여기서 잡지 않는다 — BaseException이라 `except
    OpenAIError`에 걸리지 않고, 취소는 오류가 아니라 그냥 스트림이 끊기는 것이다.
    """
    client = _client(resolved.provider)
    kwargs = _build_kwargs(req, resolved)
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}  # 마지막 청크에 usage 동봉(토큰 로깅)

    try:
        chunks = await client.chat.completions.create(**kwargs)
    except OpenAIError as exc:
        raise _translate(exc, resolved) from exc

    model = req.model
    usage = TokenUsage()
    finish_reason: str | None = None
    try:
        async for chunk in chunks:
            # 모델·usage는 choices가 빈 청크(특히 usage 전용 마지막 청크)에도 오므로
            # choices 가드보다 먼저 수집한다.
            model = getattr(chunk, "model", None) or model
            if getattr(chunk, "usage", None) is not None:
                usage = _usage_of(chunk)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            # 글자인지 확인하고 쓴다 — `_text_of`와 같은 규칙이다. 글자가 아닌 값(공급자에
            # 따라 조각을 블록 목록으로 주기도 한다)을 그대로 흘리면 사용자 화면에 그 표기가
            # 뜨거나, 이어 붙이는 쪽에서 터진다. 이상한 값은 조용히 버린다.
            reason = getattr(choices[0], "finish_reason", None)
            if isinstance(reason, str) and reason:
                finish_reason = reason
            delta = getattr(getattr(choices[0], "delta", None), "content", None)
            if isinstance(delta, str) and delta:
                yield TextDelta(delta)
    except OpenAIError as exc:
        # 일부를 이미 흘려보낸 뒤여도 중립 예외로 바꿔 던진다 — 호출부가 SSE error 이벤트로 옮긴다.
        raise _translate(exc, resolved) from exc
    except httpx.TimeoutException as exc:
        # 스트림이 열린 뒤의 읽기 타임아웃. SDK는 요청 단계의 타임아웃만 APITimeoutError로 접고
        # 반복 중의 것은 그대로 올려보낸다 — 아래 분기에 맡기면 같은 시간 초과가 발생 시점에 따라
        # provider_timeout / provider_unavailable로 갈린다.
        raise LlmTimeout(
            f"{type(exc).__name__}: {exc}", provider=resolved.provider, model=resolved.model
        ) from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        # SDK 밖으로 새는 **전송·파싱 오류만** 접는다. 스트림 반복은 SDK가 try/finally로만 감싸
        # (except 없음) 연결 끊김(httpx)·SSE 줄의 깨진 JSON이 번역 없이 통과한다 — 그대로 두면
        # 채팅은 error 이벤트도 못 내고 끊기고, 선택지·판정은 실패를 흡수하지 못해 500이 된다.
        #
        # 모든 예외를 잡지 않는 이유: 우리 코드의 결함(오타·형 실수)까지 여기서 접으면 그것이
        # "공급자 장애"로 기록돼 원인 추적이 헛돈다(STYLEGUIDE §4).
        # BaseException도 잡지 않는다: 취소(CancelledError)·조기 종료(GeneratorExit)는 오류가 아니다.
        raise LlmUnavailable(
            f"{type(exc).__name__}: {exc}", provider=resolved.provider, model=resolved.model
        ) from exc
    finally:
        # 스트림을 명시적으로 닫아 커넥션을 바로 반납한다. SDK도 자체 finally에서 닫지만 그것은
        # 내부 제너레이터가 정리될 때(GC 시점) 실행돼, 사용자가 채팅 도중 창을 닫으면 반납이
        # 늦어진다 — 흔한 경로라 여기서 결정적으로 닫는다.
        try:
            await chunks.close()
        except Exception:  # 정리 실패가 원래 오류·취소를 덮으면 안 된다 — 삼키되 로그로 남긴다.
            logger.warning("스트림 정리에 실패했다 — 원래 결과를 그대로 둔다", exc_info=True)
    yield StreamCompleted(
        model=model,
        provider=resolved.provider,
        usage=usage,
        finish_reason=finish_reason,
    )
