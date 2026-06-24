import json
from dataclasses import dataclass

from fastapi import HTTPException, status
from openai import AsyncOpenAI, OpenAIError

from src.core.config import settings
from src.schemas.response_meta import StoryResponseMeta
from src.schemas.story_compile import StoryCompileRequest, StoryCompileResponse, StorySpec
from src.services.prompt import COMPILE_VERSION, build_compile_prompt, build_refill_prompt
from src.services.story_compile_render import spec_to_response


@dataclass
class LlmUsage:
    """LLM 호출이 실제로 쓴 메타(로깅용). model은 응답이 돌려준 실제 모델명이다."""

    model: str
    input_tokens: int | None
    output_tokens: int | None


def _add_tokens(a: int | None, b: int | None) -> int | None:
    """토큰 수를 합산한다(재호출 합산용). 둘 다 None이면 None, 아니면 누락을 0으로 본다."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)

_client = AsyncOpenAI(
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_api_url,
    timeout=90.0,  # 무한 대기 방지 — 정상 컴파일은 ~30초, 초과 시 APITimeoutError → 502
)

# DeepSeek V4를 비추론으로 호출한다(thinking 비활성). 컴파일은 창작 태스크라
# 추론 모드가 출력 외국어 오염·평면화를 일으켜 비추론이 더 안정적이었다(KNK-208 벤치).
_THINKING_DISABLED = {"thinking": {"type": "disabled"}}

# 출력이 무한정 길어지지 않도록 상한. 컴파일 명세 JSON(인물 최대 5명)도 충분히 담긴다.
_MAX_TOKENS = 6144

# 빈 필수 필드를 채우기 위한 부분 재호출 최대 횟수. 초과하면 502로 막는다.
_MAX_REFILL = 2

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


async def _complete_json(system_prompt: str, user_prompt: str) -> tuple[dict, LlmUsage]:
    """LLM을 호출해 (JSON dict, 사용 메타)를 반환한다. 호출·빈응답·파싱 오류를 502로 변환한다.

    로깅용 메타는 응답에서 직접 뽑는다 — model은 `response.model`(실제 쓴 모델), 토큰은
    `response.usage`(없으면 None). 이 한 곳이 메타의 출처라 모델을 바꿔도 따로 손댈 게 없다.
    """
    try:
        response = await _client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=_MAX_TOKENS,
            extra_body=_THINKING_DISABLED,
        )
        content = response.choices[0].message.content
        if not content:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM이 빈 응답을 반환했습니다.",
            )
        parsed = json.loads(_strip_code_fence(content))
        if not isinstance(parsed, dict):
            # json_object를 무시하고 배열·스칼라를 반환한 경우. dict 가정이 깨지면
            # 호출부(compile_story 등)에서 AttributeError로 500이 나므로 여기서 502로 막는다.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM이 JSON 객체를 반환하지 않았습니다.",
            )
        usage = LlmUsage(
            model=response.model or settings.deepseek_model,
            # 토큰 필드 누락 시에도 'null 폴백' 계약을 지키도록 getattr로 방어한다.
            input_tokens=getattr(response.usage, "prompt_tokens", None),
            output_tokens=getattr(response.usage, "completion_tokens", None),
        )
        return parsed, usage
    except OpenAIError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM 플랫폼 연동 중 오류가 발생했습니다: {str(e)}",
        )
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM이 올바른 JSON 형식을 반환하지 않았습니다.",
        )


async def generate_storylines(system_prompt: str, user_prompt: str) -> tuple[dict, LlmUsage]:
    """스토리라인 생성 — (결과 dict, 사용 메타)를 반환한다. 메타 조립은 엔드포인트가 한다."""
    return await _complete_json(system_prompt, user_prompt)


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
            for k in ("name", "personality", "tone", "motivation", "attitude_to_user"):
                if _is_empty(c.get(k)):
                    missing.append(f"prompt_settings.character_setting[{i}].{k}")

    ur = _as_dict(ps.get("user_role_setting"))
    for k in ("name", "role", "background", "personality"):  # preference는 선택
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

    return missing


def _block_of(path: str) -> str:
    """빈 필드 경로를 재호출/병합 단위인 블록명으로 환원한다."""
    if path.startswith("meta"):
        return "meta"
    if path.startswith("start"):
        return "start"
    if path.startswith("suggested_inputs"):
        return "suggested_inputs"
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
        else:  # meta / start / suggested_inputs (top-level)
            data[b] = refill[b]


def _inject_genre(data: dict, genre_tags: list[str]) -> None:
    """genre는 예외 경로 — LLM 출력이 아니라 입력 태그를 정본으로 덮어쓴다(3.3·3.4)."""
    if isinstance(data.get("meta"), dict):
        data["meta"]["genre"] = ", ".join(genre_tags)


async def compile_story(request: StoryCompileRequest) -> StoryCompileResponse:
    """시점 A-1: 희소 입력을 스토리 명세로 컴파일해 백엔드 계약(nested 통글)으로 반환한다.

    흐름: LLM 세분 JSON → genre 주입 → 빈 필수키 검증 → 빈 블록만 부분 재호출(최대 2회)
    → StorySpec 파싱 → nested 통글 변환. PromptCompiler 추상 경계(6절). HTTP 노출은 KNK-78.
    """
    system_prompt, user_prompt = build_compile_prompt(
        request.selected_storyline,
        request.extra_info,
        request.genre_tags,
        request.protagonist_tags,
        request.supporting_tags,
    )
    data, usage = await _complete_json(system_prompt, user_prompt)
    _inject_genre(data, request.genre_tags)

    # 토큰은 본호출+재호출을 합산하고, model은 본호출 응답값을 쓴다(로깅 메타).
    input_tokens, output_tokens = usage.input_tokens, usage.output_tokens

    # 빈 필수 필드가 있으면 해당 블록만 다시 채운다(최대 _MAX_REFILL회).
    missing = _find_missing_keys(data)
    attempts = 0
    while missing and attempts < _MAX_REFILL:
        attempts += 1
        blocks = sorted({_block_of(p) for p in missing})
        refill_system, refill_user = build_refill_prompt(
            user_prompt,
            json.dumps(data, ensure_ascii=False),
            blocks,
        )
        refill, refill_usage = await _complete_json(refill_system, refill_user)
        input_tokens = _add_tokens(input_tokens, refill_usage.input_tokens)
        output_tokens = _add_tokens(output_tokens, refill_usage.output_tokens)
        _merge_blocks(data, refill, blocks)
        _inject_genre(data, request.genre_tags)
        missing = _find_missing_keys(data)

    if missing:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"재호출 후에도 컴파일 결과에 필수 필드가 비어 있습니다: {missing}",
        )

    try:
        spec = StorySpec(**data)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"컴파일 결과가 스토리 명세 형식과 맞지 않습니다: {e}",
        )

    response = spec_to_response(spec)
    response.meta = StoryResponseMeta(
        model=usage.model,
        prompt_versions={"COMPILE": COMPILE_VERSION},
        provider=settings.llm_provider,
        input_token_count=input_tokens,
        output_token_count=output_tokens,
        retry_count=attempts,  # 부분 재호출 횟수(0~_MAX_REFILL)
    )
    return response
