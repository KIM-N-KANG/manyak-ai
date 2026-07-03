"""다음 행동 선택지 생성 (시점 B, 본문과 분리된 2번째 호출).

본문 스트림이 끝난 뒤, 방금 생성된 장면(ai_output)을 이어 주인공이 취할 다음 행동
선택지 3개를 별도 LLM 호출로 만든다. 본문 호출과 토큰을 다투지 않게 분리했고, json
출력이라 마커 파싱이 없다.

**항상 정확히 3개를 보장**한다(본문에 선택지 블록이 끼어 안 나오던 '빈 추천창' 문제 해결):
1) 프롬프트가 3개 요청 → 2) `len<3`이면 "이미 가진 것 제외, 모자란 개수만" 누적 재호출
(최대 _MAX_REFILL회) → 3) 그래도 모자라면 준비된 폴백으로 채워 정확히 3개로 보정.
호출이 통째로 실패해도(타임아웃·파싱오류) 흡수해 폴백으로 3개를 채운다 — 선택지 때문에
턴이 실패하지 않는다.

NEXT-ACTIONS-TEMPLATE.md는 6레이어 조립에 들어가지 않는 독립 프롬프트다(STORYLINES/
COMPILE 템플릿과 같은 위상).
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI, OpenAIError

from src.core.config import settings
from src.core.sentry import FEATURE_CHOICE_GENERATION, capture_ai_exception
from src.schemas.chat_turn import ChatTurnRequest
from src.services.prompt_meta import read_version

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "prompt" / "chat" / "NEXT-ACTIONS-TEMPLATE.md"

# 버전은 frontmatter가 SSOT(KNK-228). 적재 키는 백엔드 ai_call_logs 연속성을 위해
# NEXT_ACTIONS를 유지한다(§0-1 적재 이벤트·§0-4 버전 키 예시). 상수명도 키에 맞춰 둔다.
NEXT_ACTIONS_VERSION = read_version(_TEMPLATE_PATH)

# 정확히 3개 — 와이어 계약(choices)·기존 UI가 3개를 기대한다.
_CHOICES_COUNT = 3
# 누적 재호출 최대 횟수(첫 호출 제외). 초과하면 폴백으로 보정한다.
_MAX_REFILL = 2
# 선택지는 짧으므로 출력 상한을 작게 둔다.
_MAX_TOKENS = 512
# DeepSeek V4 비추론 호출(thinking 비활성) — 본문 호출과 동일 정책.
_THINKING_DISABLED = {"thinking": {"type": "disabled"}}

# 누적·재호출조차 3개를 못 채운 '최후 안전망'. 장면을 가리지 않는 중립 행동 3개(서로 다름).
# 이 폴백이 쓰이면 프롬프트가 약하다는 신호이므로 로깅한다(거의 발동하지 않아야 정상).
_FALLBACK = (
    "*잠시 멈춰 주변을 살핀다*",
    "*한 걸음 물러서며 거리를 둔다*",
    "*상대의 반응을 기다리며 침묵한다*",
)

_client = AsyncOpenAI(
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_api_url,
    timeout=60.0,  # 짧은 생성이라 본문(90s)보다 짧게 둔다.
)


def _load_template(path: Path) -> tuple[str, str]:
    """템플릿을 `## [SYSTEM]` / `## [USER]` 두 블록으로 분할한다(prompt.py와 동일 규약)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
        _, after_system = text.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
        return system_raw.strip().removesuffix("---").strip(), user_raw.strip()
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"선택지 프롬프트 로드/파싱 실패: {path.name}: {e}")


_SYSTEM, _USER_TEMPLATE = _load_template(_TEMPLATE_PATH)


@dataclass
class ChoicesResult:
    """선택지 생성 결과(로깅 메타 합산용). choices는 항상 정확히 3개다."""

    choices: list[str]
    input_tokens: int | None
    output_tokens: int | None
    retry_count: int  # 누적 재호출 횟수(0~_MAX_REFILL)
    model: str


def _add_tokens(a: int | None, b: int | None) -> int | None:
    """토큰 합산(누적 호출용). 둘 다 None이면 None, 아니면 누락을 0으로 본다."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _strip_code_fence(text: str) -> str:
    """LLM이 JSON을 ```json ... ``` 코드펜스로 감싼 경우 제거한다."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _format_history(req: ChatTurnRequest) -> str:
    """History를 읽기 쉬운 텍스트로 만든다(역할은 한글 라벨). 비면 빈 문자열."""
    label = {"USER": "주인공", "ASSISTANT": "이야기"}
    lines = [f"{label.get(h.role, h.role)}: {h.content}" for h in req.history]
    return "\n\n".join(lines)


def _build_user(req: ChatTurnRequest, ai_output: str) -> str:
    """[USER] 블록의 자리표시자를 요청 재료 + 방금 생성된 본문으로 치환한다."""
    ss = req.story_settings
    repl = {
        "{{장르}}": req.genre,
        "{{world_setting}}": ss.world_setting,
        "{{character_setting}}": ss.character_setting,
        "{{user_role_setting}}": ss.user_role_setting,
        "{{rule_setting}}": ss.rule_setting,
        "{{summary}}": req.summary or "(아직 없음)",
        "{{history}}": _format_history(req) or "(없음)",
        "{{user_input}}": req.user_input,
        "{{ai_output}}": ai_output,
    }
    text = _USER_TEMPLATE
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def _refill_suffix(existing: list[str], need: int) -> str:
    """누적 재호출용 꼬리말 — 이미 가진 선택지를 알려주고 겹치지 않는 N개만 요청한다."""
    joined = " / ".join(existing)
    return (
        f"\n\n참고: 이미 다음 선택지를 만들었다(중복 금지) — {joined}\n"
        f"이와 방향이 겹치지 않는 새로운 선택지 {need}개만 더 만들어, "
        f'같은 형식({{"choices":[...]}})으로 출력하라.'
    )


def _accumulate(collected: list[str], seen: set[str], raw: object) -> None:
    """LLM이 준 후보에서 문자열만 정제(공백·빈값·중복 제거)해 누적한다."""
    if not isinstance(raw, list):
        return
    for item in raw:
        if isinstance(item, str):
            s = item.strip()
            if s and s not in seen:
                collected.append(s)
                seen.add(s)


async def _call(system: str, user: str) -> tuple[list, str, int | None, int | None]:
    """선택지 호출 1회 → (choices 리스트, model, in_tokens, out_tokens). 실패 시 예외."""
    response = await _client.chat.completions.create(
        model=settings.deepseek_chat_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=_MAX_TOKENS,
        extra_body=_THINKING_DISABLED,
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("LLM이 빈 응답을 반환했습니다.")
    data = json.loads(_strip_code_fence(content))
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list):
        raise ValueError("응답에 choices 배열이 없습니다.")
    usage = response.usage
    return (
        choices,
        response.model or settings.deepseek_chat_model,
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )


async def generate_choices(req: ChatTurnRequest, ai_output: str) -> ChoicesResult:
    """방금 장면(ai_output)을 이어 다음 행동 선택지를 만든다 — **항상 정확히 3개 보장**.

    첫 호출 → (부족하면) 누적 재호출(최대 _MAX_REFILL회, 기존 제외·모자란 개수만) →
    그래도 부족하면 폴백으로 채워 정확히 3개로 보정. 호출 실패는 흡수해 폴백으로 메운다.
    """
    base_user = _build_user(req, ai_output)
    collected: list[str] = []
    seen: set[str] = set()
    input_tokens: int | None = None
    output_tokens: int | None = None
    model = settings.deepseek_chat_model
    attempt = 0  # 0=첫 호출, 1·2=재호출. 종료 시 값이 곧 재호출 횟수다.

    while True:
        need = _CHOICES_COUNT - len(collected)
        user = base_user if attempt == 0 else base_user + _refill_suffix(collected, need)
        try:
            raw, model, in_tok, out_tok = await _call(_SYSTEM, user)
            input_tokens = _add_tokens(input_tokens, in_tok)
            output_tokens = _add_tokens(output_tokens, out_tok)
            _accumulate(collected, seen, raw)
        except (OpenAIError, json.JSONDecodeError, ValueError) as e:
            # 한 번의 호출이 터져도 턴을 깨지 않는다 — Sentry로만 보고하고 다음 시도/폴백으로 간다.
            logger.warning("선택지 호출 실패(시도 %d): %s", attempt, e)
            capture_ai_exception(
                e,
                feature=FEATURE_CHOICE_GENERATION,
                model=settings.deepseek_chat_model,
                prompt_versions={"NEXT_ACTIONS": NEXT_ACTIONS_VERSION},
            )
        if len(collected) >= _CHOICES_COUNT or attempt >= _MAX_REFILL:
            break
        attempt += 1

    # 결정적 보정 — 누적·재호출로도 모자라면 폴백으로 채워 정확히 3개를 만든다(LLM 없음, 절대 실패 없음).
    if len(collected) < _CHOICES_COUNT:
        logger.warning(
            "선택지 %d개만 확보 — 폴백으로 %d개 채움(프롬프트 점검 신호)",
            len(collected),
            _CHOICES_COUNT - len(collected),
        )
        for f in _FALLBACK:
            if len(collected) >= _CHOICES_COUNT:
                break
            if f not in seen:
                collected.append(f)
                seen.add(f)

    return ChoicesResult(
        choices=collected[:_CHOICES_COUNT],  # 초과분은 잘라 정확히 3개
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        retry_count=attempt,
        model=model,
    )
