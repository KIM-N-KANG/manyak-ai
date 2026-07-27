"""주요 사건·엔딩 판정 (시점 B, 본문과 분리된 사후 판정 호출 — KNK-484).

본문 스트림이 끝난 뒤, 방금 생성된 장면(ai_output)과 요청의 사건·엔딩 재료로
목표 사건 상태·이번 턴 완결 사건·엔딩 도달을 별도 LLM 호출(json)로 판정한다.
판정 결과는 completed 판정 메타(targetMainEvent·occurredMainEventName·endingName)가
되어 백엔드가 채팅 상태로 저장한다(D11 — 상태는 백엔드, 판정은 AI).

설계 결정(plan-1 승인): 본문 마커 파싱(KNK-194에서 폐기)도, 본문 생성 전 선행
판정(첫 토큰 지연)도 아닌 **사후 판정 전용 호출**이다. 선택지 호출과 병렬로 돌아
지연 증가를 최소화한다(엔드포인트가 gather).

**판정 실패가 턴을 깨지 않는다** — 호출·파싱이 실패하면 흡수하고 3필드 null로
돌아간다(선택지 폴백과 같은 원칙). 재료가 아예 없는 턴(사건·엔딩 없는 스토리)은
호출 자체를 스킵해 비용·지연이 0이다(하위호환).
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from src.core.config import settings
from src.core.sentry import FEATURE_CHAT_RESPONSE, capture_ai_exception
from src.schemas.chat_turn import ChatTurnRequest, TargetMainEventOut
from src.services import llm
from src.services.chat_assembler import format_main_events, format_target_main_event
from src.services.llm.base import LlmError, LlmRequest
from src.services.prompt_meta import read_version

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "prompt" / "chat" / "JUDGEMENT-TEMPLATE.md"

# 버전은 frontmatter가 SSOT(KNK-228). 적재 키는 신설 JUDGEMENT — prompt_versions는
# 키→정수 dict 계약이라 키 추가는 백엔드(JSONB) 하위호환이다.
JUDGEMENT_VERSION = read_version(_TEMPLATE_PATH)

# 판정 출력은 작은 JSON 하나뿐이라 상한을 작게 둔다.
_MAX_TOKENS = 256
# 이 호출의 제한 시간(초). 짧은 생성이라 본문(90초)보다 짧게 둔다(선택지와 동일).
# **호출마다 반드시 넘긴다** — 비우면 상한이 SDK 기본값(10분)으로 늘어난다.
_TIMEOUT_SECONDS = 60.0

# LLM 호출은 공통 통로(src.services.llm)를 통한다(KNK-673) — 클라이언트 생성·추론 모드 같은
# 회사 문법은 이 파일에서 사라졌다.


def _load_template(path: Path) -> tuple[str, str]:
    """템플릿을 `## [SYSTEM]` / `## [USER]` 두 블록으로 분할한다(chat_choices와 동일 규약)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
        _, after_system = text.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
        return system_raw.strip().removesuffix("---").strip(), user_raw.strip()
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"판정 프롬프트 로드/파싱 실패: {path.name}: {e}")


_SYSTEM, _USER_TEMPLATE = _load_template(_TEMPLATE_PATH)


@dataclass
class JudgementResult:
    """판정 결과(completed 판정 메타 + 로깅 합산용). 실패·스킵이면 3필드 전부 None."""

    target_main_event: TargetMainEventOut | None
    occurred_main_event_name: str | None
    ending_name: str | None
    input_tokens: int | None
    output_tokens: int | None


_EMPTY = JudgementResult(None, None, None, None, None)


# 주요 사건·목표 사건 포맷은 조립기(chat_assembler)와 공용이다 — 표기가 갈리지 않게
# 한 곳(assembler)에서 import해 재사용한다(F3). 엔딩은 판정 재료에 epilogue를 싣지
# 않으므로 아래 _format_endings로 따로 둔다(의도적 분기).
def _format_occurred(req: ChatTurnRequest) -> str:
    return "\n".join(f"- {n}" for n in req.occurred_main_event_names) or "(없음)"


def _format_endings(req: ChatTurnRequest) -> str:
    # epilogue는 본문 생성용 가이드라 판정 재료에 싣지 않는다(토큰 절약).
    return "\n".join(
        f"- 이름: {e.name} / 달성 조건: {e.achievement_condition}" for e in req.endings
    ) or "(없음)"


def _build_user(req: ChatTurnRequest, ai_output: str) -> str:
    """[USER] 블록의 자리표시자를 사건·엔딩 재료 + 방금 생성된 본문으로 치환한다."""
    repl = {
        "{{main_events}}": format_main_events(req.main_events),
        "{{target_main_event}}": format_target_main_event(req.target_main_event),
        "{{occurred_main_event_names}}": _format_occurred(req),
        "{{endings}}": _format_endings(req),
        "{{user_input}}": req.user_input,
        "{{ai_output}}": ai_output,
    }
    text = _USER_TEMPLATE
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def _strip_code_fence(text: str) -> str:
    """LLM이 JSON을 ```json ... ``` 코드펜스로 감싼 경우 제거한다(chat_choices와 동일)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _sanitize(req: ChatTurnRequest, data: dict) -> JudgementResult:
    """LLM 판정을 계약으로 보정한다(D7 — 형식·유효성은 코드 담보, 판정 내용은 프롬프트).

    목록에 없는 이름·형식 위반 값은 조용히 null로 무효화한다 — 백엔드가 해소 불가능한
    이름을 저장하지 않게 하는 최소 방어다. 무효화는 프롬프트 점검 신호로 로깅한다.
    """
    event_names = {e.name for e in req.main_events}
    ending_names = {e.name for e in req.endings}

    target: TargetMainEventOut | None = None
    raw_target = data.get("target_main_event")
    if isinstance(raw_target, dict):
        name = raw_target.get("name")
        turns = raw_target.get("progress_turns")
        # occurred 가드(아래)와 대칭 — 이전 턴들에서 이미 완결된 사건은 목표로 되보고해도
        # 무효화한다. 아니면 완결된 사건이 백엔드에 저장돼 다음 턴 STORY 슬롯까지 되돌아와
        # "끝난 사건을 향해 빌드업"하라고 지시하게 된다(#1).
        if (
            isinstance(name, str)
            and name in event_names
            and name not in req.occurred_main_event_names
            and isinstance(turns, int)
            and turns >= 0
        ):
            target = TargetMainEventOut(name=name, progress_turns=turns)
        else:
            logger.warning("판정 target 무효화(목록 밖·이미 완결·형식 위반): %r", raw_target)

    occurred = data.get("occurred_main_event_name")
    if not (isinstance(occurred, str) and occurred in event_names):
        if occurred is not None:
            logger.warning("판정 occurred 무효화(목록 밖 이름): %r", occurred)
        occurred = None
    elif occurred in req.occurred_main_event_names:
        # 이미 거쳐온 사건의 재완결 보고는 버린다(백엔드 유니크 제약과 정합).
        logger.warning("판정 occurred 무효화(이미 완결된 사건): %r", occurred)
        occurred = None

    ending = data.get("ending_name")
    if not (isinstance(ending, str) and ending in ending_names):
        if ending is not None:
            logger.warning("판정 ending 무효화(후보 밖 이름): %r", ending)
        ending = None

    # 완결 직후 상태 일관성 — 완결된 사건을 계속 목표로 들고 있지 않게 한다(프롬프트 규칙의 코드 보강).
    if occurred and target and target.name == occurred:
        target = None

    return JudgementResult(
        target_main_event=target,
        occurred_main_event_name=occurred,
        ending_name=ending,
        input_tokens=None,  # 호출부에서 채운다
        output_tokens=None,
    )


async def generate_judgement(req: ChatTurnRequest, ai_output: str) -> JudgementResult:
    """방금 턴을 사후 판정한다. 재료가 없으면 호출 없이 스킵, 실패하면 흡수해 null.

    단일 호출이다(재호출 없음) — 판정은 폴백으로 지어낼 수 없는 값이라, 실패는
    '판정 없음(null)'이 가장 안전한 결과다.
    """
    if not req.main_events and not req.endings:
        return _EMPTY  # 사건·엔딩 없는 스토리(재료 없음) — 비용·지연 0

    # 이 호출이 어느 공급자로 갈지는 부르기 전에 정한다 — 등록부 해석이 실패하면 LLM을
    # 부르기도 전에 막혀 헛돈이 안 나가고, 네 호출부(본문·선택지·판정·스토리)가 모두 같은
    # 자리에서 provider를 구해 읽는 사람이 규칙을 한 번만 익히면 된다(KNK-674 리뷰 L1).
    #
    # **아래 except가 이 예외를 흡수해 주지는 않는다.** 여기서 나는 예외는 LlmConfigError
    # 하나인데 LlmError를 상속하지 않아, 어디에 두든 밖으로 샌다. 그 경로는 기동 검사
    # (`validate_startup`)가 막는 몫이다 — CHAT_MODEL이 등록부에 없으면 서버가 안 뜬다.
    provider = llm.provider_of(settings.chat_model)
    start = time.monotonic()
    try:
        result_llm = await llm.complete(
            LlmRequest(
                model=settings.chat_model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _build_user(req, ai_output)},
                ],
                max_tokens=_MAX_TOKENS,
                timeout=_TIMEOUT_SECONDS,
                json_mode=True,
            )
        )
        # 통로는 응답 모양이 깨져도 예외를 던지지 않고 빈 문자열을 준다 — 아래가 그대로 받는다.
        if not result_llm.text:
            raise ValueError("LLM이 빈 응답을 반환했습니다.")
        data = json.loads(_strip_code_fence(result_llm.text))
        if not isinstance(data, dict):
            raise ValueError("판정 응답이 JSON 객체가 아닙니다.")
        result = _sanitize(req, data)
        result.input_tokens = result_llm.usage.input_tokens
        result.output_tokens = result_llm.usage.output_tokens
        return result
    except (LlmError, json.JSONDecodeError, ValueError) as e:
        # 판정 실패가 턴을 깨지 않는다 — Sentry로만 보고하고 null로 돌아간다. 예외가 밖으로
        # 새면 gather가 그것을 전파해 턴 자체가 깨진다.
        #
        # 전송 오류(LlmError)는 통로가 접어 준 것이고, 내용물 오류(깨진 JSON·빈 응답·비객체)는
        # 여기 남는다. 예전에는 응답 껍데기가 깨졌을 때의 IndexError·AttributeError도 잡았지만,
        # 이제 통로가 그런 응답을 빈 문자열로 정규화해 위 `if not result_llm.text`가 받는다 —
        # 결과(판정 null)는 같고 경로만 옮겨졌다. **두 예외를 다시 넣지 않는다**: 우리 코드의
        # 오타까지 "판정 없음"으로 조용히 덮인다.
        # 별도 feature 신설은 관측 카탈로그(6-analytics) 개정 사안이라 chat_response로 묶는다.
        logger.warning("판정 호출 실패(흡수 — 메타 null): %s", e)
        capture_ai_exception(
            e,
            feature=FEATURE_CHAT_RESPONSE,
            provider=provider,
            model=settings.chat_model,
            prompt_versions={"JUDGEMENT": JUDGEMENT_VERSION},
            retry_count=0,  # 단일 호출 — 재호출 없음(D12)
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        return _EMPTY
