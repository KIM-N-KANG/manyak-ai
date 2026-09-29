"""TypeSafe 선택형 판정 어댑터(KNK-1448). HTTP 단발 호출, 원문 비수집.

공식 계약: https://docs.typesafe.ai/api
질문 결과는 그대로 반환하며 감정 순위·강도·임계값은 해석하지 않는다.
"""

import asyncio
import json
import math

import httpx

from src.core import langfuse
from src.services.llm import registry
from src.services.llm.base import (
    ADAPTER_TYPESAFE_API,
    PROVIDER_TYPESAFE,
    ChoiceAnswer,
    ChoiceQuestion,
    EvaluationRequest,
    EvaluationResult,
    LlmBadRequest,
    LlmConfigError,
    LlmInvalidResponse,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
    ResolvedModel,
    TokenUsage,
)

_MAX_RESPONSE_BYTES = 1_000_000


def check_supported(resolved: ResolvedModel) -> None:
    """기동·호출 전에 지원 모델과 접속 주소를 검사한다. 키 부재는 호출 시 검사한다."""
    if resolved.adapter != ADAPTER_TYPESAFE_API or resolved.provider != PROVIDER_TYPESAFE:
        raise LlmConfigError("evaluate는 TypeSafe 판정 모델만 지원합니다.")
    creds = registry.credentials(resolved.provider)
    try:
        url = httpx.URL(creds.base_url or "")
    except httpx.InvalidURL:
        raise LlmConfigError("TYPESAFE_API_URL 형식이 잘못됐습니다.") from None
    if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or url.path not in ("", "/")):
        raise LlmConfigError("TYPESAFE_API_URL은 인증정보·경로·쿼리가 없는 HTTPS 주소여야 합니다.")


def _request_body(req: EvaluationRequest) -> bytes:
    """지원 범위 밖 요청은 네트워크·과금 전에 거부한다."""
    if (type(req.timeout) not in (int, float) or not math.isfinite(req.timeout)
            or req.timeout <= 0):
        raise LlmConfigError("판정 timeout은 유한한 양수여야 합니다.")
    if not isinstance(req.state, (str, dict, list)):
        raise LlmConfigError("판정 state는 문자열·객체·배열이어야 합니다.")
    if not isinstance(req.questions, dict) or not req.questions:
        raise LlmConfigError("판정 questions는 비어 있지 않은 객체여야 합니다.")
    questions = {}
    for name, question in req.questions.items():
        if (not isinstance(name, str) or not name.strip()
                or not isinstance(question, ChoiceQuestion)
                or not isinstance(question.instructions, str) or not question.instructions.strip()
                or not isinstance(question.criteria, dict) or not 2 <= len(question.criteria) <= 255
                or any(not isinstance(option, str) or not option.strip() or not isinstance(desc, str)
                       for option, desc in question.criteria.items())):
            raise LlmConfigError("판정 질문은 문자열 지시와 2~255개 문자열 후보 설명이 필요합니다.")
        questions[name] = {"type": "choice", "instructions": question.instructions,
                           "criteria": question.criteria}
    try:
        return json.dumps({"model": req.model, "state": req.state, "questions": questions},
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise LlmConfigError("판정 요청을 JSON으로 변환할 수 없습니다.") from None


def _probability(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def _usage_details(data: object) -> dict[str, int]:
    """답변 검증 전에 받은 사용량을 보존한다. 누락·잘못된 값은 추정하지 않는다."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {
        target: usage[source]
        for source, target in (("input_tokens", "input"), ("output_tokens", "output"))
        if type(usage.get(source)) is int and usage[source] >= 0
    }


def _parse_response(data: object, req: EvaluationRequest, resolved: ResolvedModel) -> EvaluationResult:
    """후보 누락·추가, 잘못된 선택값·확률·모델을 원문 없이 거부한다."""
    def invalid() -> LlmInvalidResponse:
        return LlmInvalidResponse("TypeSafe 판정 응답 형식이 잘못됐습니다.",
                                  provider=resolved.provider, model=resolved.model)

    if not isinstance(data, dict) or data.get("model") != resolved.model:
        raise invalid()
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(req.questions):
        raise invalid()
    parsed = {}
    for name, question in req.questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise invalid()
        probs = answer.get("probabilities")
        choice = answer.get("choice")
        confidence = answer.get("confidence")
        if (not isinstance(probs, dict) or set(probs) != set(question.criteria)
                or not all(_probability(p) for p in probs.values())
                # API는 확률을 반올림해 반환한다(실측에서 0.99 합계도 반환).
                or not math.isclose(sum(probs.values()), 1.0, rel_tol=0, abs_tol=0.010001)
                or not isinstance(choice, str) or choice not in probs
                or probs[choice] != max(probs.values()) or not _probability(confidence)):
            raise invalid()
        parsed[name] = ChoiceAnswer(choice=choice, probabilities=dict(probs), confidence=confidence)
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise invalid()
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise invalid()
    return EvaluationResult(
        answers=parsed, model=resolved.model, provider=resolved.provider,
        usage=TokenUsage(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")),
    )


async def evaluate(req: EvaluationRequest, resolved: ResolvedModel) -> EvaluationResult:
    """응답 전체에 시간 제한을 적용한다. 재시도·리다이렉트 없이 취소를 전파한다."""
    check_supported(resolved)
    body = _request_body(req)
    creds = registry.credentials(resolved.provider)
    if (not creds.api_key or not creds.api_key.isascii() or not creds.api_key.isprintable()
            or any(char.isspace() for char in creds.api_key)):
        raise LlmConfigError("TYPESAFE_API_KEY가 없거나 형식이 잘못됐습니다.")
    error_args = {"provider": resolved.provider, "model": resolved.model}
    with langfuse.observe_generation(
        "LLM 판정", model=resolved.model,
        model_parameters={"question_count": len(req.questions), "timeout": req.timeout, "max_retries": 0},
    ) as generation:
        try:
            async with asyncio.timeout(req.timeout):
                async with httpx.AsyncClient(
                    timeout=req.timeout, follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST", (creds.base_url or "").rstrip("/") + "/v1/systemone",
                        content=body,
                        headers={"Authorization": f"Bearer {creds.api_key}", "Content-Type": "application/json"},
                    ) as response:
                        if response.status_code == 429:
                            raise LlmRateLimited("TypeSafe 요청량 제한", **error_args)
                        if response.status_code == 400:
                            raise LlmBadRequest("TypeSafe 판정 요청 거부", **error_args)
                        if response.status_code != 200:
                            raise LlmUnavailable("TypeSafe 판정 호출 실패", **error_args)
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            raw.extend(chunk)
                            if len(raw) > _MAX_RESPONSE_BYTES:
                                raise LlmInvalidResponse("TypeSafe 응답 크기 제한 초과", **error_args)
            data = json.loads(raw)
        except (TimeoutError, httpx.TimeoutException):
            raise LlmTimeout("TypeSafe 판정 시간 초과", **error_args) from None
        except httpx.HTTPError:
            raise LlmUnavailable("TypeSafe 판정 전송 실패", **error_args) from None
        except (ValueError, UnicodeError):
            raise LlmInvalidResponse("TypeSafe 응답 JSON이 잘못됐습니다.", **error_args) from None
        usage_details = _usage_details(data)
        if usage_details:
            generation.finish(usage_details=usage_details)
        result = _parse_response(data, req, resolved)
        generation.finish(output={"answer_count": len(result.answers)})
        return result
