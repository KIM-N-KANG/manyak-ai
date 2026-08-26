import base64
import json
import logging
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import HTTPException, status

from src.core.config import settings
from src.core.sentry import (
    ERROR_INVALID_AI_RESPONSE,
    ERROR_PROVIDER_BAD_REQUEST,
    ERROR_PROVIDER_RATE_LIMITED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_SCHEMA_VALIDATION_FAILED,
    ERROR_UNEXPECTED,
    FEATURE_CHARACTER_IMAGE,
    FEATURE_STORY_COMPLETION,
    FEATURE_STORYLINE_GENERATION,
    capture_ai_exception,
    classify_error_code,
)
from src.schemas.response_meta import StoryResponseMeta
from src.schemas.story import CharacterInput, StoryItem
from src.schemas.story_compile import (
    CharacterImageOut,
    Ending,
    StoryCompileRequest,
    StoryCompileResponse,
    StorySpec,
)
from src.services import llm
from src.services.image.base import PROVIDER_OPENAI
from src.services.image.prompt import CHARACTER_IMAGE_VERSION
from src.services.llm.base import LlmError, LlmRequest
from src.services.prompt import (
    COMPILE_GEMINI_VERSION,
    COMPILE_VERSION,
    GENDER_KO,
    STORYLINES_VERSION,
    build_compile_prompt,
    build_refill_prompt,
    build_storylines_refill_prompt,
)
from src.services.story_compile_render import spec_to_response

logger = logging.getLogger(__name__)


class _InvalidAiResponse(Exception):
    """LLM이 빈 응답·비객체 JSON을 반환 — invalid_ai_response로 분류한다."""


@dataclass
class LlmUsage:
    """LLM 호출이 실제로 쓴 메타(로깅용). model은 응답이 돌려준 실제 모델명이다.

    retry_count는 invalid 응답으로 같은 호출을 다시 부른 횟수(KNK-312). 토큰은 실패한
    시도분까지 합산한다 — 실패해도 과금은 됐으므로 ai_call_logs 적재값이 실비용과 맞아야 한다.
    """

    model: str
    input_tokens: int | None
    output_tokens: int | None
    # 이 호출이 실제로 어느 공급자로 나갔는지(KNK-674). 전역 설정값이 아니라 모델 이름을
    # 등록부가 해석한 값이라, 경로마다 다른 회사를 써도 적재값이 어긋나지 않는다.
    #
    # **기본값을 두지 않는다** — 두면 새 호출부가 조용히 그 값을 물려받아, 없애려던 전역
    # 폴백이 이름만 바꿔 되살아난다.
    #
    # **이름으로만 넘길 수 있게 한다(kw_only).** 이 칸을 retry_count 앞에 새로 끼워 넣었기
    # 때문에, 그냥 두면 예전 습관대로 순서만 적은 `LlmUsage("m", 1, 2, 3)`이 **에러 없이**
    # 숫자 3을 공급자 이름으로 받아들이고 재호출 횟수는 0이 된다. kw_only면 그런 호출이
    # 그 자리에서 TypeError로 막힌다(KNK-674 리뷰 L2).
    provider: str = field(kw_only=True)
    retry_count: int = 0


def _add_tokens(a: int | None, b: int | None) -> int | None:
    """토큰 수를 합산한다(재호출 합산용). 둘 다 None이면 None, 아니면 누락을 0으로 본다."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)

# LLM 호출은 공통 통로(src.services.llm)를 통한다(KNK-672). 어느 회사 SDK로 어떤 인자를
# 보낼지는 모델 등록부와 어댑터가 정하므로, 여기서는 "무엇을 원하는지"만 넘긴다 —
# 클라이언트 생성·추론 모드(thinking) 같은 회사 문법은 이 파일에서 사라졌다.

# 스토리라인은 기존 출력 상한을 유지하고, 컴파일은 Terra medium 실측 조건과 같은
# 16,384를 쓴다. Terra의 한도는 추론 토큰과 본문이 함께 사용한다.
_STORYLINES_MAX_TOKENS = 6_144
_COMPILE_MAX_TOKENS = 16_384

# 생성 온도. 스토리라인 블라인드 독자 평가에서 0.75가 0.5보다 읽기 품질(감정 밀도·창의성)
# 우세이면서 파싱 성공률·속도는 동급이고, 기본값 1.0보다 분량 폭주·파싱 실패 꼬리위험이
# 낮았다(KNK-269 검증). 모델이 temperature를 받지 않으면 어댑터가 인자를 보내지 않는다.
_TEMPERATURE = 0.75

# 빈 필수 필드를 채우기 위한 부분 재호출 최대 횟수. 초과하면 502로 막는다.
_MAX_REFILL = 2

# invalid 응답 재호출(KNK-312)의 전체 시간 상한(초). 백엔드 storylines read timeout이 90초라
# (manyak-server StoryAiClient), 이 시각을 넘긴 재호출은 성공해도 백엔드가 이미 끊은 뒤다.
# 60초 = 90초에서 재호출 1번(평시 11~17초)의 여유를 남긴 값. 초과 시 남은 재호출을 포기한다.
_INVALID_RETRY_DEADLINE_SECONDS = 60.0

# 재호출을 포함한 전체 호출 예산(초) = 백엔드 read timeout. 각 시도의 호출 타임아웃을
# "예산의 남은 시간"으로 줄여, 60초 직전에 시작한 재호출이 자체 90초 타임아웃으로 총
# 149초까지 끌지 못하게 한다(Codex 리뷰 P2). 첫 시도는 남은 시간=90초라 기존과 동일하다.
_TOTAL_CALL_BUDGET_SECONDS = 90.0

# 502 detail은 사용자 노출용 메시지만 담는다(AN-4-7) — provider 원문은 Sentry로만 보낸다(AN-4-10).
_DETAIL_BY_CODE = {
    ERROR_PROVIDER_TIMEOUT: "LLM 응답 시간이 초과되었습니다.",
    ERROR_PROVIDER_RATE_LIMITED: "LLM 요청이 일시적으로 제한되었습니다.",
    ERROR_PROVIDER_BAD_REQUEST: "LLM 요청이 거부되었습니다.",
    ERROR_PROVIDER_UNAVAILABLE: "LLM 연동 중 오류가 발생했습니다.",
    ERROR_INVALID_AI_RESPONSE: "LLM이 올바른 형식의 응답을 반환하지 않았습니다.",
}

# prompt_settings 하위에 속하는 재호출 블록명. 병합 시 prompt_settings 안에 덮어쓴다.
_PROMPT_SETTINGS_BLOCKS = {
    "world_setting",
    "plot_setting",
    "rule_setting",
    "tone_setting",
    "length_ratio",
    "character_setting",
    "user_role_setting",
}


def _strip_code_fence(text: str) -> str:
    """LLM이 JSON을 ```json ... ``` 코드 펜스로 감싼 경우 제거한다."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # 첫 줄(``` 또는 ```json)과 마지막 ``` 라인을 제거
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


async def _complete_json(
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    *,
    label: str = "compile",
    feature: str = FEATURE_STORY_COMPLETION,
    prompt_versions: dict | None = None,
    max_invalid_retries: int = 0,
    validate: Callable[[dict], None] | None = None,
    max_tokens: int = _COMPILE_MAX_TOKENS,
) -> tuple[dict, LlmUsage]:
    """LLM을 호출해 (JSON dict, 사용 메타)를 반환한다. 호출·빈응답·파싱 오류를 502로 변환한다.

    model이 None이면 컴파일용 story_compile_model로 폴백한다. 기본 인자에 settings 값을
    직접 두면 import 시점에 고정돼 런타임 오버라이드(테스트 등)가 반영되지 않으므로 호출
    시점에 해석한다. 응답 속도가 중요한 경로(스토리라인)는 호출 측에서 flash 모델을 넘겨
    덮어쓴다(KNK-215). label은 진단 로깅에서 호출 종류를 구분하는 용도다(KNK-222).

    로깅용 메타는 통로가 돌려준 결과에서 뽑는다 — model은 실제 쓴 모델명, 토큰은 사용량
    (없으면 None). 이 한 곳이 메타의 출처라 모델을 바꿔도 따로 손댈 게 없다.

    max_invalid_retries(KNK-312): 응답이 왔지만 내용물이 못 쓸 것일 때(invalid_ai_response —
    깨진 JSON·빈 응답·비객체)만 같은 요청을 그 횟수까지 다시 부른다. 재호출은 대개 다른
    (유효한) 출력을 낸다. provider 오류(타임아웃·429·5xx)는 여기서 다시
    부르지 않는다 — 전송 실패는 어댑터 아래의 SDK 재시도가 이미 맡고 있고, 타임아웃은 90초
    예산을 이미 소진한 뒤라 다시 불러도 백엔드가 기다려주지 않는다.

    validate: 파싱이 성공한 dict의 내용 계약을 추가 검증하는 훅. 위반 시 _InvalidAiResponse를
    던지면 위 invalid 재호출과 같은 경로를 탄다 — 파싱은 됐지만 못 쓸 응답(스키마 불일치 등)이
    루프 밖(응답 조립)에서 500으로 터지는 것을 루프 안에서 잡기 위함이다(Sentry PYTHON-FASTAPI-A).
    """
    resolved_model = model if model is not None else settings.story_compile_model
    # 이 호출이 어느 공급자로 갈지는 부르기 전에 정해진다 — 실패해서 결과가 없어도 Sentry
    # 태그를 채울 수 있어야 한다(KNK-674).
    provider = llm.provider_of(resolved_model)
    attempts = 0  # invalid 응답으로 다시 부른 횟수(첫 호출은 0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    overall_start = time.monotonic()  # 재호출 포함 전체 경과의 기준(60초 상한 판정용)
    while True:
        start = time.monotonic()
        try:
            result = await llm.complete(
                LlmRequest(
                    model=resolved_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_tokens,
                    # 시도별 타임아웃 = 전체 예산(90초)의 남은 시간 — 60초 직전에 시작한 재호출이
                    # 총 90초를 넘겨 끌지 못하게 한다(Codex P2). 첫 시도는 남은 시간이 90초라 종전과 같다.
                    # **반드시 채운다** — 비우면 상한이 SDK 기본값(10분)으로 늘어난다.
                    timeout=max(1.0, _TOTAL_CALL_BUDGET_SECONDS - (start - overall_start)),
                    temperature=_TEMPERATURE,
                    json_mode=True,
                )
            )
            # 진단: 호출별 소요시간·입출력 토큰·캐시 적중을 남겨 병목(재호출/출력 decode)을 실측한다.
            usage = result.usage
            logger.info(
                "LLM[%s] %.1fs in=%s out=%s cache_hit=%s",
                label,
                time.monotonic() - start,
                usage.input_tokens if usage.input_tokens is not None else "?",
                usage.output_tokens if usage.output_tokens is not None else "?",
                (
                    usage.cache_read_input_tokens
                    if usage.cache_read_input_tokens is not None
                    else "?"
                ),
            )
            # 토큰은 파싱 전에 합산한다 — 이 시도가 파싱에서 실패해도 과금은 됐으므로,
            # 재호출 성공 시 메타가 실패 시도분까지 실비용을 반영해야 한다(컴파일 refill 합산과 동일 원칙).
            input_tokens = _add_tokens(input_tokens, usage.input_tokens)
            output_tokens = _add_tokens(output_tokens, usage.output_tokens)
            # 통로는 응답 모양이 깨져도 예외를 던지지 않고 빈 문자열을 준다 — 아래 invalid 경로가
            # 그대로 받아 재호출(KNK-312)을 태운다.
            content = result.text
            if not content:
                raise _InvalidAiResponse("LLM이 빈 응답을 반환했습니다.")
            parsed = json.loads(_strip_code_fence(content))
            if not isinstance(parsed, dict):
                # json_object를 무시하고 배열·스칼라를 반환한 경우. dict 가정이 깨지면
                # 호출부(compile_story 등)에서 AttributeError로 500이 나므로 여기서 막는다.
                raise _InvalidAiResponse("LLM이 JSON 객체를 반환하지 않았습니다.")
            if validate is not None:
                validate(parsed)  # 계약 위반이면 _InvalidAiResponse → 아래 재호출 경로
            return parsed, LlmUsage(
                # 응답이 돌려준 실제 모델명. 비어 오면 요청 이름으로 채우는 폴백은 통로가 한다.
                model=result.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider=result.provider,
                retry_count=attempts,
            )
        except (LlmError, json.JSONDecodeError, _InvalidAiResponse) as exc:
            # 실패를 한 곳에서 모아 Sentry에 보고하고(AN-4) error_code별 502로 바꾼다. 502 detail에는
            # provider 원문(str(e))을 싣지 않는다 — 내부 상세는 Sentry로만 보낸다(AN-4-7·4-10).
            #
            # 두 종류를 함께 잡는다. **전송 오류**(LlmError — 타임아웃·429·요청거부·연결실패)는
            # 통로가 회사 SDK 예외를 접어 준 것이고, **내용물 오류**(깨진 JSON·빈/비객체 응답)는
            # 여기 남는다. 내용물 오류만 재호출(KNK-312) 대상이라 둘을 섞으면 안 된다.
            #
            # 응답 껍데기가 깨진 경우(빈 choices·message 없음)는 예전에 여기서 IndexError·
            # AttributeError로 잡았지만, 이제 통로가 그것을 빈 본문으로 정규화해 위 `if not content`가
            # 받는다 — 결과(502 invalid_ai_response)는 같고 경로만 한 단계 위로 옮겨졌다.
            # **두 예외를 다시 넣지 않는다**: 그 그물은 우리 코드의 오타(NameError 계열 제외)까지
            # invalid로 오분류해 돈 드는 재호출 2회를 태운다(KNK-672 리뷰).
            error_code = (
                ERROR_INVALID_AI_RESPONSE
                if isinstance(exc, (json.JSONDecodeError, _InvalidAiResponse))
                else classify_error_code(exc)
            )
            # 재호출로 넘어가는 실패도 Sentry에 남긴다 — 최종 성공 여부와 무관하게
            # invalid 응답의 발생 빈도를 계속 관측해야 프롬프트·모델 개선의 근거가 된다.
            capture_ai_exception(
                exc,
                feature=feature,
                provider=provider,
                error_code=error_code,
                model=resolved_model,
                prompt_versions=prompt_versions,
                retry_count=attempts,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            if error_code == ERROR_INVALID_AI_RESPONSE and attempts < max_invalid_retries:
                elapsed = time.monotonic() - overall_start
                if elapsed < _INVALID_RETRY_DEADLINE_SECONDS:
                    attempts += 1
                    logger.info(
                        "LLM[%s] invalid 응답 — 재호출 %d/%d", label, attempts, max_invalid_retries
                    )
                    continue
                # 재호출 예산이 남아도 시간이 없으면 포기 — 성공해도 백엔드(90초)가 이미 끊은 뒤다.
                logger.info("LLM[%s] invalid 응답이나 %.0f초 경과 — 재호출 포기", label, elapsed)
            http_exc = HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_DETAIL_BY_CODE.get(error_code, "LLM 처리 중 오류가 발생했습니다."),
            )
            # 실패까지의 재호출 횟수를 예외에 실어 보낸다 — 엔드포인트가 실패 트레이스에도
            # 실제 값을 기록할 수 있게(Codex 리뷰 F2). provider 오류처럼 재호출이 없었으면 0.
            http_exc.retry_count = attempts
            raise http_exc from exc


def _validate_storylines(data: dict) -> None:
    """스토리라인 응답의 stories 계약(정확히 3편 × 항목 스키마 × 추천 3개)을 검증한다.

    JSON 파싱이 성공해도 항목이 응답 스키마와 어긋나면(예: recommended_infos 누락)
    엔드포인트의 응답 조립에서 ValidationError(500)로 터졌다(Sentry PYTHON-FASTAPI-A).
    _complete_json의 validate 훅으로 재호출 루프 안에서 실행돼, 위반 시 다른 invalid
    응답과 같은 재호출 → 소진 시 502 경로를 탄다(KNK-312).

    응답 자체가 못 쓸 것(깨진 JSON·편수 부족·스키마 불일치)만 여기서 본다. 인물 누락은
    편 단위로 고칠 수 있어 전체 재호출이 아니라 부분 재호출로 처리한다(_missing_name_indexes).
    """
    stories = data.get("stories")
    if not isinstance(stories, list) or len(stories) != 3:
        raise _InvalidAiResponse("stories가 정확히 3편이 아닙니다.")
    for i, item in enumerate(stories):
        if not isinstance(item, dict):
            raise _InvalidAiResponse(f"stories[{i}]가 JSON 객체가 아닙니다.")
        try:
            parsed = StoryItem(**item)
        except (TypeError, ValueError) as exc:
            raise _InvalidAiResponse(f"stories[{i}]가 응답 스키마와 맞지 않습니다.") from exc
        if len(parsed.recommended_infos) != 3:
            raise _InvalidAiResponse(f"stories[{i}]의 recommended_infos가 3개가 아닙니다.")


def _missing_name_indexes(data: dict, required_names: tuple[str, ...]) -> list[int]:
    """이름 지은 주변 인물이 빠진 편의 번호(0-based)를 모은다(KNK-833·KNK-840).

    입력 인물은 세 편 모두에 그 이름 그대로 나와야 한다. 주인공 이름은 1인칭 본문이라
    등장을 강제하지 않고, 이름을 비운 인물은 LLM이 지은 이름을 알 수 없어 대상이 아니다.

    빠진 편만 골라 돌려주는 이유는, 세 편을 통째로 다시 사면 잘 나온 편까지 버리기
    때문이다 — 실측에서 한 편만 인물이 빠지는 경우가 나왔다. _validate_storylines가
    3편·dict·필드를 이미 보장한 뒤에만 부른다.
    """
    if not required_names:
        return []
    missing: list[int] = []
    for i, item in enumerate(data["stories"]):
        body = unicodedata.normalize("NFC", str(item.get("storyline", "")))
        if any(n not in body for n in required_names):
            missing.append(i)
    return missing


def _merge_storylines(data: dict, refill: dict, indexes: list[int]) -> None:
    """부분 재호출 응답의 편을 원본 자리에 끼워 넣는다.

    재호출은 `{"stories": [{"id": 2, ...}]}`처럼 다시 쓴 편만 담아 오고, id가 곧 자리
    번호다(1-based). 요청하지 않은 자리나 범위 밖 id는 무시한다 — 모델이 엉뚱한 편을
    덮어써 잘 나온 편을 망치지 못하게 한다.
    """
    items = refill.get("stories")
    if not isinstance(items, list):
        return
    allowed = set(indexes)
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            continue
        pos = raw_id - 1
        if pos in allowed:
            data["stories"][pos] = item


def _normalize_storyline_ids(data: dict) -> None:
    """stories의 id를 등장 순서대로 1·2·3으로 덮어쓴다(스펙 §5-2 계약 값 보장).

    LLM이 id를 중복·범위 밖(예: [1,1,99])으로 줘도, 재호출·502로 벌하지 않고 코드가
    정본 값으로 교정한다 — 장르(_inject_genre)와 같은 '계약 값은 코드가 담보'(D7) 패턴.
    _validate_storylines가 이미 stories=3편·dict를 보장한 뒤에만 호출한다."""
    for i, item in enumerate(data["stories"]):
        item["id"] = i + 1


async def generate_storylines(
    system_prompt: str,
    user_prompt: str,
    required_names: list[str] | None = None,
) -> tuple[dict, LlmUsage]:
    """스토리라인 생성 — (결과 dict, 사용 메타)를 반환한다. 메타 조립은 엔드포인트가 한다.

    응답 속도가 사용자 체감에 직결돼 flash 모델을 쓴다(KNK-215, pro 대비 ~2배 빠름).
    label="storylines"로 진단 로깅을 구분한다(KNK-222).

    invalid 응답이면 최대 2회 재호출한다(KNK-312). 실측된 주 원인은 본문 속 대사
    인용부호를 JSON 이스케이프 없이 출력해 파싱이 깨지는 확률적 실수라(Sentry
    PYTHON-FASTAPI-5, finish_reason=stop), 같은 요청을 다시 부르면 대부분 해소된다.
    파싱이 성공해도 stories 계약 위반(_validate_storylines)이면 같은 재호출을 탄다.
    호출이 유난히 느렸던 요청은 재호출해도 백엔드 대기 한도(90초)를 넘기므로, 전체
    경과 60초를 넘긴 시점부터는 재호출을 포기한다(_INVALID_RETRY_DEADLINE_SECONDS).

    검증을 통과한 응답은 id를 순서대로 1·2·3으로 교정해 반환한다(_normalize_storyline_ids) —
    id 값 어긋남은 무해한 이탈이라 재호출·502로 벌하지 않고 코드가 정본 값을 박는다(D7).

    required_names(사용자가 이름 지은 주변 인물, KNK-833)가 빠지면 **빠진 편만** 다시
    받는다(KNK-840, 최대 2회). 전체 재호출로 되돌리지 않는 이유는 잘 나온 편까지 버리게
    되고, 출력이 3배라 값과 대기 시간도 그만큼 늘기 때문이다(실측: 한 편만 빠지는 경우가
    나옴). meta의 retry_count는 전체 재호출과 부분 재호출을 합한 수이고, 토큰도 합산한다.
    """
    result, usage = await _complete_json(
        system_prompt,
        user_prompt,
        model=settings.storylines_model,
        label="storylines",
        feature=FEATURE_STORYLINE_GENERATION,
        prompt_versions={"STORYLINES": STORYLINES_VERSION},
        max_invalid_retries=2,
        validate=_validate_storylines,
        max_tokens=_STORYLINES_MAX_TOKENS,
    )
    _normalize_storyline_ids(result)

    names = tuple(required_names or ())
    input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
    refills = 0
    missing = _missing_name_indexes(result, names)
    while missing and refills < _MAX_REFILL:
        refills += 1
        logger.info("storylines 부분 재호출 #%d 대상 편=%s", refills, [i + 1 for i in missing])
        refill_system, refill_user = build_storylines_refill_prompt(
            user_prompt,
            json.dumps(result, ensure_ascii=False),
            [i + 1 for i in missing],
        )
        refill, refill_usage = await _complete_json(
            refill_system,
            refill_user,
            model=settings.storylines_model,
            label=f"storylines-refill#{refills}",
            feature=FEATURE_STORYLINE_GENERATION,
            prompt_versions={"STORYLINES": STORYLINES_VERSION},
            max_tokens=_STORYLINES_MAX_TOKENS,
        )
        input_tokens = _add_tokens(input_tokens, refill_usage.input_tokens)
        output_tokens = _add_tokens(output_tokens, refill_usage.output_tokens)
        # 병합 전 원본을 복사해 둔다 — 재호출이 깨진 편을 데려와 검증이 실패하면
        # 이름만 빠졌을 뿐 계약은 유효했던 원본으로 되돌린다(502로 보내지 않는다).
        backup = [dict(s) for s in result["stories"]]
        _merge_storylines(result, refill, missing)
        try:
            _validate_storylines(result)
        except _InvalidAiResponse:
            logger.info("storylines 부분 재호출 #%d 결과가 계약을 깨 원본으로 되돌림", refills)
            result["stories"] = backup
        _normalize_storyline_ids(result)
        missing = _missing_name_indexes(result, names)

    if missing:
        exc = _InvalidAiResponse(
            f"부분 재호출 후에도 입력 주변 인물이 빠진 편이 {len(missing)}편 남았습니다."
        )
        _raise_storylines_invalid(exc, usage, refills)

    total = LlmUsage(
        usage.model,
        input_tokens,
        output_tokens,
        provider=usage.provider,
        retry_count=usage.retry_count + refills,
    )
    return result, total


def _raise_storylines_invalid(exc: _InvalidAiResponse, usage: LlmUsage, refills: int) -> None:
    """부분 재호출로도 못 고친 응답을 502로 막는다(Sentry 보고 포함)."""
    capture_ai_exception(
        exc,
        feature=FEATURE_STORYLINE_GENERATION,
        provider=usage.provider,
        error_code=ERROR_INVALID_AI_RESPONSE,
        model=usage.model,
        prompt_versions={"STORYLINES": STORYLINES_VERSION},
        retry_count=usage.retry_count + refills,
    )
    http_exc = HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=_DETAIL_BY_CODE[ERROR_INVALID_AI_RESPONSE],
    )
    # 실패 트레이스에도 실제 재호출 횟수가 실리게 한다(_complete_json과 같은 관례).
    http_exc.retry_count = usage.retry_count + refills
    raise http_exc from exc


# ── 컴파일 결과 검증 (StorySpec 파싱 전 dict 단계) ──────────────────────────
# Pydantic은 빈 문자열("")을 통과시키고, 파싱이 먼저 실패하면 재호출 기회가 사라진다.
# 따라서 빈 필수 필드는 dict 단계에서 직접 탐지해 부분 재호출로 채운다.
# meta.genre(코드가 입력 태그로 덮어씀)·user_role_setting.preference(선택)는 검증 제외.


def _is_empty(value: object) -> bool:
    """필수 필드가 비었는지 판정. 공백만·빈배열·null도 빈으로 본다."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _as_dict(value: object) -> dict:
    """LLM이 객체 자리에 문자열·null 등을 줘도 .get 접근에서 터지지 않게 방어한다."""
    return value if isinstance(value, dict) else {}


def _is_valid_min_turns(value: object) -> bool:
    """min_turns가 하한(1) 이상 정수로 해석되는지 — Ending(ge=1) 강제와 맞춰,
    0·음수·비정수를 재호출 대상으로 잡아 502 대신 폴백으로 흐르게 한다(KNK-465)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 1
    if isinstance(value, float):
        return value.is_integer() and value >= 1
    if isinstance(value, str):
        # int()로 직접 해석한다 — isdigit()은 '²'·'①' 등 int()가 못 바꾸는 유니코드 숫자에도
        # True를 줘서, 검사 도중 ValueError가 새어 나가면 폴백이 아니라 500이 된다(Gemini 리뷰).
        try:
            return int(value.strip()) >= 1
        except ValueError:
            return False
    return False


def _find_missing_keys(data: dict) -> list[str]:
    """비어 있는 필수 필드의 경로 목록을 반환한다(빈 목록이면 통과).

    LLM이 타입을 어겨도(객체 자리에 문자열 등) 여기서 빈 값으로 간주해 재호출/502로
    흐르게 한다 — 500(AttributeError)으로 새지 않는다.
    """
    missing: list[str] = []

    meta = _as_dict(data.get("meta"))
    for k in ("title", "one_line_intro", "description"):
        if _is_empty(meta.get(k)):
            missing.append(f"meta.{k}")

    ps = _as_dict(data.get("prompt_settings"))
    for k in ("world_setting", "rule_setting", "tone_setting", "length_ratio"):
        if _is_empty(ps.get(k)):
            missing.append(f"prompt_settings.{k}")

    plot = _as_dict(ps.get("plot_setting"))
    for k in ("premise", "conflict"):
        if _is_empty(plot.get(k)):
            missing.append(f"prompt_settings.plot_setting.{k}")

    chars = ps.get("character_setting")
    if not isinstance(chars, list) or len(chars) == 0:
        missing.append("prompt_settings.character_setting")
    else:
        for i, raw in enumerate(chars):
            c = _as_dict(raw)
            # name의 빈값·중복은 카드 전체가 아니라 name만 고치는 전용 경로가 맡는다.
            for k in ("gender", "personality", "tone", "motivation", "attitude_to_user"):
                if _is_empty(c.get(k)):
                    missing.append(f"prompt_settings.character_setting[{i}].{k}")

    ur = _as_dict(ps.get("user_role_setting"))
    for k in ("name", "gender", "role", "background", "personality"):  # preference는 선택
        if _is_empty(ur.get(k)):
            missing.append(f"prompt_settings.user_role_setting.{k}")

    start = _as_dict(data.get("start"))
    for k in ("name", "prologue", "start_situation"):
        if _is_empty(start.get(k)):
            missing.append(f"start.{k}")

    si = data.get("suggested_inputs")
    if not isinstance(si, list) or len(si) != 3:  # 정확히 3개 필수
        missing.append("suggested_inputs")
    else:
        for i, s in enumerate(si):
            if _is_empty(s):
                missing.append(f"suggested_inputs[{i}]")

    events = data.get("main_events")
    if not isinstance(events, list) or not (3 <= len(events) <= 5):  # 3~5개 필수
        missing.append("main_events")
    else:
        for i, raw in enumerate(events):
            ev = _as_dict(raw)
            for k in ("name", "description", "key_sentence"):
                if _is_empty(ev.get(k)):
                    missing.append(f"main_events[{i}].{k}")

    endings = data.get("endings")
    # 엔딩은 정상 3개를 목표로 재호출을 유도한다. 3개가 아니거나 빈 필드·하한 미달(0·음수)·비정수 min_turns면 누락.
    # (재호출 후에도 못 채우면 compile_story가 502가 아니라 빈 배열로 폴백한다 — KNK-465.)
    if not isinstance(endings, list) or len(endings) != 3:
        missing.append("endings")
    else:
        for i, raw in enumerate(endings):
            en = _as_dict(raw)
            for k in ("name", "achievement_condition", "epilogue"):
                if _is_empty(en.get(k)):
                    missing.append(f"endings[{i}].{k}")
            if not _is_valid_min_turns(en.get("min_turns")):
                missing.append(f"endings[{i}].min_turns")

    return missing


def _block_of(path: str) -> str:
    """빈 필드 경로를 재호출/병합 단위인 블록명으로 환원한다."""
    if path.startswith("meta"):
        return "meta"
    if path.startswith("start"):
        return "start"
    if path.startswith("suggested_inputs"):
        return "suggested_inputs"
    if path.startswith("main_events"):
        return "main_events"
    if path.startswith("endings"):
        return "endings"
    if path.startswith("prompt_settings."):
        rest = path[len("prompt_settings."):]
        return rest.split(".")[0].split("[")[0]
    return path


def _merge_blocks(data: dict, refill: dict, blocks: list[str]) -> None:
    """부분 재호출 응답(refill)의 블록을 원본 data의 올바른 위치에 덮어쓴다."""
    for b in blocks:
        if b not in refill:
            continue
        if b in _PROMPT_SETTINGS_BLOCKS:
            # LLM이 prompt_settings를 객체가 아닌 문자열·null로 줬다면 setdefault가
            # 그 잘못된 값을 반환해 item 할당에서 TypeError(500)가 난다 — dict로 보정한다.
            if not isinstance(data.get("prompt_settings"), dict):
                data["prompt_settings"] = {}
            data["prompt_settings"][b] = refill[b]
        else:  # meta / start / suggested_inputs / main_events / endings (top-level)
            data[b] = refill[b]


def _inject_genre(data: dict, genre_tags: list[str]) -> None:
    """genre는 예외 경로 — LLM 출력이 아니라 입력 태그를 정본으로 덮어쓴다(spec/story/2-COMPILE.md §4-3)."""
    if isinstance(data.get("meta"), dict):
        data["meta"]["genre"] = ", ".join(genre_tags)


def _inject_protagonist(data: dict, protagonist: CharacterInput) -> None:
    """사용자가 입력한 주인공 이름·성별을 주인공 프로필에 정본으로 덮어쓴다(KNK-838).

    사용자가 정한 값은 LLM 출력에 맡기지 않고 코드가 담보한다(장르 덮어쓰기와 같은
    원칙, 5-ai-server.md §5-3-3 D7). 비운 항목은 LLM이 지은 값을 그대로 둔다.
    성별은 계약 값("MALE"·"FEMALE")이 아니라 통글에 실리는 한국어로 바꿔 쓴다.
    """
    ur = _as_dict(data.get("prompt_settings")).get("user_role_setting")
    if not isinstance(ur, dict):
        return  # 블록 자체가 없으면 refill이 채운 뒤 다음 주입 때 덮어쓴다
    if protagonist.name:
        ur["name"] = protagonist.name
    if protagonist.gender:
        ur["gender"] = GENDER_KO[protagonist.gender]


def _endings_incomplete(data: dict) -> bool:
    """엔딩이 '파싱 가능한 정확히 3개'가 아니면 True(폴백 대상).

    _find_missing_keys는 재호출 유도용 얕은 검사라, 타입 오류(min_turns 비정수 등)까지
    한 번에 걸러 502를 막으려면 실제 Ending 파싱을 시도해 최종 판정한다(KNK-465).
    """
    endings = data.get("endings")
    if not isinstance(endings, list) or len(endings) != 3:
        return True
    try:
        for raw in endings:
            Ending(**_as_dict(raw))
    except (TypeError, ValueError):
        return True
    return False


_CHARACTER_CARDS_PATH = "prompt_settings.character_setting"
_INPUT_CHARACTER_ID_FIELD = "input_character_id"
_CHARACTER_APPEARANCE_FIELDS = (
    "age",
    "body",
    "face",
    "hair",
    "outfit",
    "visual_identity",
)


def _input_character_id(index: int) -> str:
    """요청의 주변 인물 순번을 LLM 중간 JSON에서 식별할 안정적인 값으로 바꾼다."""
    return f"input-{index + 1}"


def _input_character_ids_incomplete(data: dict, input_count: int) -> bool:
    """입력 인물 표시가 각각 정확히 한 카드에 있는지 확인한다."""
    if input_count == 0:
        return False
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        return True

    expected = {_input_character_id(index) for index in range(input_count)}
    counts = {value: 0 for value in expected}
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        value = raw.get(_INPUT_CHARACTER_ID_FIELD)
        if value is None:
            continue
        if not isinstance(value, str) or value not in expected:
            return True
        counts[value] += 1
    return any(count != 1 for count in counts.values())


def _inject_supporting_character_names(
    data: dict,
    supporting_characters: list[CharacterInput],
) -> None:
    """내부 표시로 사용자 입력 카드를 찾아, 사용자가 정한 이름을 최종값으로 덮어쓴다."""
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        return
    by_id = {
        _input_character_id(index): character
        for index, character in enumerate(supporting_characters)
    }
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        source = by_id.get(raw.get(_INPUT_CHARACTER_ID_FIELD))
        if source is not None and source.name:
            raw["name"] = source.name


def _input_character_indexes(data: dict, input_count: int) -> set[int]:
    """중복 이름을 고칠 때 보호할 사용자 입력 카드의 배열 위치를 반환한다."""
    expected = {_input_character_id(index) for index in range(input_count)}
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        return set()
    return {
        index
        for index, raw in enumerate(cards)
        if isinstance(raw, dict) and raw.get(_INPUT_CHARACTER_ID_FIELD) in expected
    }


def _remove_input_character_ids(data: dict) -> None:
    """내부 식별자는 검증이 끝난 뒤 제거해 백엔드 계약으로 새지 않게 한다."""
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        return
    for raw in cards:
        if isinstance(raw, dict):
            raw.pop(_INPUT_CHARACTER_ID_FIELD, None)


def _missing_required_characters(data: dict, required_names: tuple[str, ...]) -> bool:
    """사용자가 이름 지은 주변 인물이 인물 카드(character_setting)에 전원 있는지 본다(KNK-833).

    빠져 있으면 본호출 전체를 다시 사는 대신 카드 블록만 부분 재호출(refill) 대상으로
    삼는다 — 컴파일은 가장 비싼 호출이라 잘 나온 나머지 블록을 보존하는 쪽이 싸다.
    카드 name에는 이름 뒤에 호칭이 붙을 수 있어 포함 여부로 보고, 이름을 비운 인물은
    LLM이 지은 이름을 알 수 없어 검증 대상이 아니다.
    """
    if not required_names:
        return False
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        cards = []
    # 카드 사이를 구분자로 이어, 이름이 카드 경계에 걸쳐 우연히 맞는 오탐을 막는다.
    # 문자열 이름만 대조한다 — null·배열을 str()로 바꾸면 "None"·"['서린']" 같은
    # 글자가 생겨 검증이 잘못 통과하고, 회복 기회(refill) 없이 뒤 단계 502로 죽는다.
    card_names = " / ".join(
        unicodedata.normalize("NFC", c["name"])
        for c in cards
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    )
    return any(n not in card_names for n in required_names)


def _find_character_field_repairs(
    data: dict,
    protected_name_indexes: set[int] | None = None,
) -> dict[int, tuple[str, ...]]:
    """인물 카드 전체를 갈아끼우지 않고 고칠 이름·외형 필드를 찾는다.

    이름은 빈값·공백·비문자열과 앞 카드에 이미 나온 중복을 잡는다. 외형은 이미지
    생성용 선택 필드라 빈값만 잡고, 두 번의 재호출로도 못 채우면 컴파일은 살린다.
    """
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        return {}

    fields_by_index: dict[int, list[str]] = {}
    name_groups: dict[str, list[int]] = {}
    for index, raw in enumerate(cards):
        if not isinstance(raw, dict):
            continue  # 잘못된 카드 객체는 기존 character_setting 블록 재호출이 맡는다

        fields: list[str] = []
        name = raw.get("name")
        if not isinstance(name, str) or _is_empty(name):
            fields.append("name")
        else:
            normalized = unicodedata.normalize("NFC", name.strip()).casefold()
            name_groups.setdefault(normalized, []).append(index)

        for field_name in _CHARACTER_APPEARANCE_FIELDS:
            value = raw.get(field_name)
            if not isinstance(value, str) or _is_empty(value):
                fields.append(field_name)

        if fields:
            fields_by_index[index] = fields

    protected = protected_name_indexes or set()
    for indexes in name_groups.values():
        if len(indexes) < 2:
            continue
        protected_duplicates = [index for index in indexes if index in protected]
        keep = protected_duplicates[0] if protected_duplicates else indexes[0]
        for index in indexes:
            if index == keep:
                continue
            fields = fields_by_index.setdefault(index, [])
            if "name" not in fields:
                fields.insert(0, "name")

    return {index: tuple(fields) for index, fields in fields_by_index.items()}


def _merge_character_field_repairs(
    data: dict,
    refill: dict,
    requested: dict[int, tuple[str, ...]],
) -> None:
    """재호출에서 요청한 인물 필드의 비어 있지 않은 문자열만 원본에 합친다."""
    updates = refill.get("character_updates")
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(updates, list) or not isinstance(cards, list):
        return

    for raw_update in updates:
        if not isinstance(raw_update, dict):
            continue
        index = raw_update.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index not in requested:
            continue
        if not (0 <= index < len(cards)) or not isinstance(cards[index], dict):
            continue
        for field_name in requested[index]:
            value = raw_update.get(field_name)
            if isinstance(value, str) and value.strip():
                cards[index][field_name] = value.strip()


def _clear_unresolved_appearance_fields(
    data: dict,
    repairs: dict[int, tuple[str, ...]],
) -> None:
    """재호출로도 못 채운 외형을 빈 문자열로 맞춰 컴파일 본체는 살린다."""
    cards = _as_dict(data.get("prompt_settings")).get("character_setting")
    if not isinstance(cards, list):
        return
    for index, fields in repairs.items():
        if not (0 <= index < len(cards)) or not isinstance(cards[index], dict):
            continue
        for field_name in fields:
            if field_name in _CHARACTER_APPEARANCE_FIELDS:
                cards[index][field_name] = ""


async def _generate_character_images_safe(
    characters: list,
    genre_tags: list[str],
) -> list[CharacterImageOut]:
    """인물별 이미지를 생성해 base64로 변환한다. 전체 실패해도 예외를 던지지 않는다.

    이미지 생성은 컴파일의 부가물이라, 여기서 터진 예외가 컴파일 200을 502로
    만들면 안 된다. 모든 예외를 잡아 빈 배열로 폴백한다.
    """
    from src.services.image.generate_characters import generate_character_images

    try:
        results = await generate_character_images(characters, genre_tags)

        images: list[CharacterImageOut] = []
        for r in results:
            if r.image is not None:
                images.append(CharacterImageOut(
                    name=r.name,
                    image_base64=base64.b64encode(r.image.image_bytes).decode("ascii"),
                ))
            else:
                # 공급자 원문은 로그에만 남기고, 응답에는 안정적인 에러 코드만 내려보낸다.
                logger.info("인물 이미지 실패: %s — %s", r.name, r.error)
                images.append(CharacterImageOut(
                    name=r.name,
                    error=_classify_image_error(r.error),
                ))
        return images
    except Exception as exc:
        # 인물 단위 실패는 어댑터·병렬 생성기가 이미 보고했다. 여기까지 온 것은 그 그물을
        # 벗어난 예외라 따로 보고한다 — 삼키기만 하면 이미지가 통째로 비어도 아무도 모른다.
        capture_ai_exception(
            exc,
            feature=FEATURE_CHARACTER_IMAGE,
            provider=PROVIDER_OPENAI,
            error_code=ERROR_UNEXPECTED,
            model=settings.image_model,
            prompt_versions={"CHARACTER_IMAGE": CHARACTER_IMAGE_VERSION},
        )
        logger.exception("인물 이미지 생성 중 예상치 못한 오류 — 이미지 없이 반환")
        return []


def _classify_image_error(error: str | None) -> str:
    """이미지 생성 실패 사유를 안정적인 코드로 분류한다.

    공급자 원문을 응답에 그대로 노출하지 않는다(텍스트 LLM의 _DETAIL_BY_CODE와 같은 원칙).
    """
    if error is None:
        return "generation_failed"
    lower = error.lower()
    if "시간 초과" in error or "timeout" in lower:
        return "timeout"
    if (
        "속도 제한" in error
        or "rate limit" in lower
        or "rate_limit" in lower
        or "rate-limited" in lower
    ):
        return "rate_limited"
    if "거부" in error or "rejected" in lower or "safety" in lower:
        return "rejected"
    if "외형 필드 부족" in error:
        return "appearance_missing"
    return "generation_failed"


async def compile_story(request: StoryCompileRequest) -> StoryCompileResponse:
    """시점 A-1: 희소 입력을 스토리 명세로 컴파일해 백엔드 계약(nested 통글)으로 반환한다.

    흐름: LLM 세분 JSON → genre·주인공 이름/성별 주입 → 빈 필수키·사용자 인물 카드 검증(KNK-833) →
    모자란 블록과 이름·외형 필드를 한 번에 부분 재호출(최대 2회) → 엔딩 미완성 시 빈 배열 폴백(KNK-465)
    → StorySpec 파싱 → nested 통글 변환.
    PromptCompiler 추상 경계는 spec/chat/4-SERVICE-IMPLEMENTATION.md §6.
    """
    compile_provider = llm.provider_of(settings.story_compile_model)
    system_prompt, user_prompt, version_key = build_compile_prompt(
        request.selected_storyline,
        request.additional_info,
        request.genre_tags,
        request.protagonist,
        request.supporting_characters,
        request.lorebooks,
        provider=compile_provider,
    )
    prompt_version = (
        COMPILE_GEMINI_VERSION if version_key == "COMPILE_GEMINI" else COMPILE_VERSION
    )
    data, usage = await _complete_json(
        system_prompt,
        user_prompt,
        label="compile",
        feature=FEATURE_STORY_COMPLETION,
        prompt_versions={version_key: prompt_version},
    )
    _inject_genre(data, request.genre_tags)
    _inject_protagonist(data, request.protagonist)
    _inject_supporting_character_names(data, request.supporting_characters)

    # 토큰은 본호출+재호출을 합산하고, model은 본호출 응답값을 쓴다(로깅 메타).
    input_tokens, output_tokens = usage.input_tokens, usage.output_tokens

    # 사용자가 이름 지은 주변 인물이 카드에서 빠지면 카드 블록도 refill 대상에 넣는다(KNK-833).
    required_names = tuple(c.name for c in request.supporting_characters if c.name)

    def _current_issues() -> tuple[list[str], dict[int, tuple[str, ...]]]:
        found = _find_missing_keys(data)
        if (
            _input_character_ids_incomplete(data, len(request.supporting_characters))
            and _CHARACTER_CARDS_PATH not in found
        ):
            found.append(_CHARACTER_CARDS_PATH)
        if _missing_required_characters(data, required_names) and _CHARACTER_CARDS_PATH not in found:
            found.append(_CHARACTER_CARDS_PATH)
        protected_indexes = _input_character_indexes(data, len(request.supporting_characters))
        character_fields = _find_character_field_repairs(data, protected_indexes)
        # 카드 블록을 통째로 다시 받는 차수에는 기존 index가 무효가 되므로 필드 수정을 함께 요청하지 않는다.
        if "character_setting" in {_block_of(path) for path in found}:
            character_fields = {}
        return found, character_fields

    # 빈 필수 블록과 인물 이름·외형 문제를 한 요청에 모아 다시 채운다(최대 _MAX_REFILL회).
    missing, character_fields = _current_issues()
    if missing or character_fields:
        logger.info(
            "compile 1차 응답 문제: 블록=%s, 인물필드=%s",
            missing or "없음",
            character_fields or "없음",
        )
    attempts = 0
    while (missing or character_fields) and attempts < _MAX_REFILL:
        attempts += 1
        blocks = sorted({_block_of(p) for p in missing})
        logger.info(
            "compile 재호출 #%d 대상 블록=%s, 인물필드=%s",
            attempts,
            blocks or "없음",
            character_fields or "없음",
        )
        refill_system, refill_user = build_refill_prompt(
            user_prompt,
            json.dumps(data, ensure_ascii=False),
            blocks,
            character_fields,
            provider=compile_provider,
        )
        refill, refill_usage = await _complete_json(
            refill_system,
            refill_user,
            label=f"refill#{attempts}",
            feature=FEATURE_STORY_COMPLETION,
            prompt_versions={version_key: prompt_version},
        )
        input_tokens = _add_tokens(input_tokens, refill_usage.input_tokens)
        output_tokens = _add_tokens(output_tokens, refill_usage.output_tokens)
        _merge_blocks(data, refill, blocks)
        _merge_character_field_repairs(data, refill, character_fields)
        _inject_genre(data, request.genre_tags)
        _inject_protagonist(data, request.protagonist)
        _inject_supporting_character_names(data, request.supporting_characters)
        missing, character_fields = _current_issues()
    logger.info(
        "compile 완료: LLM 호출 %d회(첫 1 + 재호출 %d), 최종 블록=%s, 최종 인물필드=%s",
        1 + attempts,
        attempts,
        missing or "없음",
        character_fields or "없음",
    )

    # 엔딩은 soft 블록(KNK-465): 엔딩 사유로는 502를 내지 않는다. 최종 판정은 _endings_incomplete가
    # 단독으로 하므로, missing에서 endings 경로를 항상 걷어내고(그래야 _find_missing_keys의 얕은 검사가
    # Pydantic 강제와 어긋나도 엉뚱한 502가 나지 않는다), 파싱 가능한 3개가 아니면 빈 배열로 폴백해 200을
    # 반환한다 — 스토리 본체·주요 사건은 살린다(선택지 폴백과 같은 원칙).
    missing = [p for p in missing if _block_of(p) != "endings"]
    if _endings_incomplete(data):
        logger.info("compile 엔딩 폴백: 재호출 후에도 엔딩 미완성 → 빈 배열([])로 반환")
        data["endings"] = []

    # 외형은 이미지 생성의 부가 입력이므로 끝까지 비어 있어도 컴파일을 살린다.
    # 이름은 저장·이미지 매칭 기준이라 빈값·중복이 남으면 필수 블록 누락과 함께 502로 막는다.
    name_repairs = {
        index: fields
        for index, fields in character_fields.items()
        if "name" in fields
    }
    if missing or name_repairs:
        exc = _InvalidAiResponse(
            f"재호출 후에도 필수 필드 누락: blocks={missing}, character_names={name_repairs}"
        )
        capture_ai_exception(
            exc,
            feature=FEATURE_STORY_COMPLETION,
            provider=usage.provider,
            error_code=ERROR_INVALID_AI_RESPONSE,
            model=usage.model,
            prompt_versions={version_key: prompt_version},
            retry_count=attempts,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="재호출 후에도 컴파일 결과에 필수 필드가 비어 있습니다.",
        ) from exc

    _clear_unresolved_appearance_fields(data, character_fields)
    _remove_input_character_ids(data)

    try:
        spec = StorySpec(**data)
    except (TypeError, ValueError) as e:
        capture_ai_exception(
            e,
            feature=FEATURE_STORY_COMPLETION,
            provider=usage.provider,
            error_code=ERROR_SCHEMA_VALIDATION_FAILED,
            model=usage.model,
            prompt_versions={version_key: prompt_version},
            retry_count=attempts,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="컴파일 결과가 스토리 명세 형식과 맞지 않습니다.",
        ) from e

    response = spec_to_response(spec)
    response.meta = StoryResponseMeta(
        model=usage.model,
        prompt_versions={
            version_key: prompt_version,
            "CHARACTER_IMAGE": CHARACTER_IMAGE_VERSION,
        },
        provider=usage.provider,
        input_token_count=input_tokens,
        output_token_count=output_tokens,
        retry_count=attempts,  # 부분 재호출 횟수(0~_MAX_REFILL)
    )

    # 인물별 이미지 병렬 생성(KNK-940). 컴파일 성공 후에 실행하며, 이미지 실패가
    # 컴파일 전체를 502로 만들지 않는다 — 이미지는 부가물이라 실패한 인물만 빈다.
    response.character_images = await _generate_character_images_safe(
        spec.prompt_settings.character_setting,
        request.genre_tags,
    )

    return response
