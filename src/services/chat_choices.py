"""다음 행동 선택지 생성 (시점 B, 전용 엔드포인트 /chat/choices의 호출 — KNK-625).

방금 생성된 장면(ai_output)을 이어 주인공이 취할 다음 행동 선택지 3개를 별도 LLM
호출로 만든다. 본문 호출과 토큰을 다투지 않게 분리했고(json 출력이라 마커 파싱 없음),
처음엔 /chat/turns 내부 2번째 호출이었으나 KNK-625에서 전용 엔드포인트로 승격됐다 —
completed가 선택지 생성을 기다리지 않는다.

**항상 정확히 3개를 보장**한다(본문에 선택지 블록이 끼어 안 나오던 '빈 추천창' 문제 해결):
1) 프롬프트가 3개 요청 → 2) `len<3`이면 "이미 가진 것 제외, 모자란 개수만" 누적 재호출
(최대 _MAX_REFILL회) → 3) 그래도 모자라면 준비된 폴백으로 채워 정확히 3개로 보정.
호출이 통째로 실패해도(타임아웃·파싱오류) 흡수해 폴백으로 3개를 채운다 — 유효한
요청에서 /chat/choices 응답은 실패하지 않는다.

CHOICES-TEMPLATE.md는 6레이어 조립에 들어가지 않는 독립 프롬프트다(STORYLINES/
COMPILE 템플릿과 같은 위상).
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.core.config import settings
from src.core.sentry import (
    ERROR_INVALID_AI_RESPONSE,
    FEATURE_CHOICE_GENERATION,
    capture_ai_exception,
)
from src.schemas.chat_turn import ChatTurnRequest
from src.services import llm
from src.services.chat_assembler import format_main_events, format_target_main_event
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.llm.base import LlmError, LlmRequest
from src.services.prompt_meta import read_version

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "prompt" / "chat" / "CHOICES-TEMPLATE.md"

# 버전은 frontmatter가 SSOT(KNK-228). 적재 키는 백엔드 ai_call_logs 연속성을 위해
# NEXT_ACTIONS를 유지한다(§0-1 적재 이벤트·§0-4 버전 키 예시). 상수명도 키에 맞춰 둔다.
NEXT_ACTIONS_VERSION = read_version(_TEMPLATE_PATH)

# 정확히 3개 — 와이어 계약(choices)·기존 UI가 3개를 기대한다.
_CHOICES_COUNT = 3
# 누적 재호출 최대 횟수(첫 호출 제외). 초과하면 폴백으로 보정한다.
_MAX_REFILL = 2
# 선택지는 짧으므로 출력 상한을 작게 둔다.
_MAX_TOKENS = 512
# 이 호출의 제한 시간(초). 짧은 생성이라 본문(90초)보다 짧게 둔다.
# **호출마다 반드시 넘긴다** — 비우면 상한이 SDK 기본값(10분)으로 늘어난다.
_TIMEOUT_SECONDS = 60.0

# 누적·재호출조차 3개를 못 채운 '최후 안전망'. 장면을 가리지 않는 중립 행동 3개(서로 다름).
# 이 폴백이 쓰이면 프롬프트가 약하다는 신호이므로 로깅한다(거의 발동하지 않아야 정상).
_FALLBACK = (
    "*잠시 멈춰 주변을 살핀다*",
    "*한 걸음 물러서며 거리를 둔다*",
    "*상대의 반응을 기다리며 침묵한다*",
)

# LLM 호출은 공통 통로(src.services.llm)를 통한다(KNK-673) — 클라이언트 생성·추론 모드 같은
# 회사 문법은 이 파일에서 사라졌다.


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
    # 이 호출이 실제로 어느 공급자로 나갔는지(KNK-674). 세 번 다 실패해 폴백으로 답할 때도
    # 채워져야 해서, 결과가 아니라 모델 이름을 등록부가 해석한 값을 쓴다.
    #
    # **이름으로만 넘길 수 있게 한다(kw_only)** — LlmUsage와 같은 이유다. 지금은 맨 끝이라
    # 순서로 넣어도 맞지만, 앞에 칸이 하나 끼는 순간 `ChoicesResult([...], 1, 2, 0, "m", "p")`가
    # **에러 없이** 값을 한 칸씩 밀어 넣는다 — 모델도 공급자도 문자열이라 아무도 못 알아챈다
    # (KNK-674 2차 리뷰 4번).
    provider: str = field(kw_only=True)


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
    lines = [
        f"{label.get(h.role, h.role)}: {strip_character_image_syntax(h.content)}"
        for h in req.history
    ]
    return "\n\n".join(lines)


def _build_user(req: ChatTurnRequest, ai_output: str) -> str:
    """[USER] 블록의 자리표시자를 요청 재료 + 방금 생성된 본문으로 치환한다.

    사건 재료 3종(KNK-485, §5-3-5 — 주요 사건·목표 사건·거쳐온 사건)은 선택지
    3구성(목표 1 + 미향 1 + 맥락 1)의 재료다. 재료가 비면 "(없음)"으로 치환된다
    (하위호환).
    """
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
        "{{ai_output}}": strip_character_image_syntax(ai_output),
        "{{main_events}}": format_main_events(req.main_events),
        "{{target_main_event}}": format_target_main_event(req.target_main_event),
        "{{occurred_main_event_names}}": "\n".join(
            f"- {n}" for n in req.occurred_main_event_names
        )
        or "(없음)",
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
    result = await llm.complete(
        LlmRequest(
            model=settings.chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=_MAX_TOKENS,
            timeout=_TIMEOUT_SECONDS,
            json_mode=True,
        )
    )
    # 통로는 응답 모양이 깨져도 예외를 던지지 않고 빈 문자열을 준다 — 아래가 그대로 받는다.
    if not result.text:
        raise ValueError("LLM이 빈 응답을 반환했습니다.")
    data = json.loads(_strip_code_fence(result.text))
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list):
        raise ValueError("응답에 choices 배열이 없습니다.")
    # model은 응답이 비어 와도 요청 이름으로 채워져 온다(폴백은 통로가 한다).
    return choices, result.model, result.usage.input_tokens, result.usage.output_tokens


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
    model = settings.chat_model
    # 이 호출이 어느 공급자로 갈지는 부르기 전에 정해진다 — 세 번 다 실패해 폴백으로 답하면
    # 성공 결과가 하나도 없어서, 결과에서 읽는 방식으로는 meta를 채울 수 없다(KNK-674).
    provider = llm.provider_of(model)
    attempt = 0  # 0=첫 호출, 1·2=재호출. 종료 시 값이 곧 재호출 횟수다.

    while True:
        need = _CHOICES_COUNT - len(collected)
        user = base_user if attempt == 0 else base_user + _refill_suffix(collected, need)
        t0 = time.monotonic()  # 이번 시도의 소요 시간 — 실패 캡처의 latency_ms 재료
        try:
            raw, model, in_tok, out_tok = await _call(_SYSTEM, user)
            input_tokens = _add_tokens(input_tokens, in_tok)
            output_tokens = _add_tokens(output_tokens, out_tok)
            _accumulate(collected, seen, raw)
        except (LlmError, json.JSONDecodeError, ValueError) as e:
            # 한 번의 호출이 터져도 선택지 응답을 깨지 않는다 — Sentry로만 보고하고 다음
            # 시도/폴백으로 간다. 예외가 밖으로 새면 폴백 보정을 못 거쳐 /chat/choices가 500이 된다.
            #
            # 전송 오류(LlmError)는 통로가 회사 SDK 예외를 접어 준 것이고, 내용물 오류(깨진
            # JSON·빈 응답·choices 없음)는 여기 남는다. 예전에는 응답 껍데기가 깨졌을 때의
            # IndexError·AttributeError도 잡았지만, 이제 통로가 그런 응답을 빈 문자열로
            # 정규화해 위 `if not result.text`가 받는다 — 결과는 같고 경로만 옮겨졌다.
            # **두 예외를 다시 넣지 않는다**: 우리 코드의 오타까지 폴백으로 조용히 덮인다.
            logger.warning("선택지 호출 실패(시도 %d): %s", attempt, e)
            capture_ai_exception(
                e,
                feature=FEATURE_CHOICE_GENERATION,
                provider=provider,
                error_code=(
                    ERROR_INVALID_AI_RESPONSE
                    if isinstance(e, (json.JSONDecodeError, ValueError))
                    else None
                ),
                model=settings.chat_model,
                prompt_versions={"NEXT_ACTIONS": NEXT_ACTIONS_VERSION},
                retry_count=attempt,  # 0=첫 호출, 1·2=재호출
                latency_ms=int((time.monotonic() - t0) * 1000),
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
        provider=provider,
    )
