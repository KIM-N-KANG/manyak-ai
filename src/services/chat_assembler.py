"""채팅 본문 프롬프트 조립 (시점 B, 매 턴, 완전 stateless).

`ChatTurnRequest`를 받아 LLM 호출용 messages 배열을 만든다. 본문 프롬프트는 두 종류다
(KNK-1622, 설정 `CHAT_PROMPT`).

- layer(기본): 6레이어 슬롯을 결정적 치환하고, 명세 4.1 배치
  ([시스템 앞] → [History] → [Depth] → [PHI])로 조립한다.
- general: 파일 하나(`prompt/chat/general/CHAT-TEMPLATE.md`)를 `## [SYSTEM]`/`## [USER]`로
  나눠 [SYSTEM] → [History] → [USER] 순으로 조립한다. 지시문이 맨 앞 하나뿐이다.

AI는 상태를 보관하지 않으며(순수 함수), 모든 재료는 매 턴 request로 받는다.

LLM 호출(`stream=True`)·SSE 엔드포인트는 별도(KNK-144)다. 이 모듈은 조립까지만 한다.
`scripts/preview_chat_prompt.py`가 시각 검증용 reference이며 동일 로직을 따른다.
"""

import re
from pathlib import Path

from src.core.config import ChatPrompt, settings
from src.schemas.chat_turn import (
    ChatHistoryItem,
    ChatStartSettings,
    ChatTurnRequest,
    EndingCandidate,
    MainEvent,
    TargetMainEvent,
)
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.prompt_meta import read_version

_LAYER_DIR = Path(__file__).parent.parent.parent / "prompt" / "chat" / "layer"
_GENERAL_PATH = Path(__file__).parent.parent.parent / "prompt" / "chat" / "general" / "CHAT-TEMPLATE.md"
_LAYER_NAMES = ("SAFETY", "CORE", "STORY", "CHARACTER", "USER", "MEMORY")


def _body_only(text: str) -> str:
    """frontmatter(--- ... ---)를 떼고 본문만 반환한다(조립기는 content만 주입).

    줄바꿈은 LF/CRLF 모두 허용한다(read_version과 동일 관례) — CRLF로 저장된
    템플릿에서 frontmatter가 본문에 새어 LLM에 전달되는 사고를 막는다(KNK-574).
    """
    m = re.match(r"^---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n(.*)$", text, re.S)
    return (m.group(1) if m else text).strip()


def _phi_core(body: str) -> str:
    """<phi_core>...</phi_core> 구간만 추출한다(PHI 재주입용; 없으면 빈 문자열)."""
    m = re.search(r"<phi_core>(.*?)</phi_core>", body, re.S)
    return m.group(1).strip() if m else ""


def _layer_path(name: str) -> Path:
    return _LAYER_DIR / f"{name}-TEMPLATE.md"


def _load_layer(name: str) -> str:
    # utf-8-sig: BOM이 있으면 떼고 읽는다 — frontmatter가 본문에 새지 않게.
    return _body_only(_layer_path(name).read_text(encoding="utf-8-sig"))


# 레이어 본문은 정적이라 모듈 로드 시 1회만 읽어 캐시한다(매 턴 재치환은 .replace로 충분).
_LAYERS = {n: _load_layer(n) for n in _LAYER_NAMES}

# 레이어 버전은 frontmatter가 SSOT다(KNK-228). 로깅용으로 레이어별 버전을 노출한다(KNK-243).
LAYER_VERSIONS = {n: read_version(_layer_path(n)) for n in _LAYER_NAMES}

# general 프롬프트 버전도 frontmatter가 SSOT다. 기록에는 `body_prompt_versions()`로 싣는다.
GENERAL_VERSION = read_version(_GENERAL_PATH)


def _selected_prompt(prompt: ChatPrompt | None) -> ChatPrompt:
    """인자가 없으면 설정 CHAT_PROMPT를 따른다. 조립과 버전 기록이 같은 규칙을 쓰게 한 곳에 둔다."""
    return prompt or settings.chat_prompt


def body_prompt_versions(prompt: ChatPrompt | None = None) -> dict[str, int]:
    """본문 호출에 실제로 쓴 프롬프트의 버전 맵(KNK-1623).

    layer면 레이어별 버전을, general이면 `{"GENERAL": n}`을 돌려준다. 백엔드는 이 맵을 키 변환
    없이 그대로 적재하므로(ai_call_logs.prompt_versions), 키만 보고 어느 프롬프트로 나온 응답인지
    구분된다. 호출부가 고쳐 써도 원본이 바뀌지 않게 새 dict를 돌려준다.
    """
    if _selected_prompt(prompt) == "general":
        return {"GENERAL": GENERAL_VERSION}
    return dict(LAYER_VERSIONS)


# [시스템 앞] 배치 순서(명세 4.1) — MEMORY는 Depth 전담이라 제외.
_SYSTEM_FRONT = ("SAFETY", "CORE", "STORY", "CHARACTER", "USER")
# [PHI] 재주입 순서(명세 4.1) — SAFETY가 생성에 가장 근접. USER는 갓모딩 방지로 제외.
_PHI_ORDER = ("CHARACTER", "STORY", "CORE", "SAFETY")

# OpenAI 호환 LLM은 소문자 role을 요구한다(ERD/스키마는 대문자 USER/ASSISTANT).
_ROLE_MAP = {"USER": "user", "ASSISTANT": "assistant"}


def _start_setting_blob(start: ChatStartSettings) -> str:
    """story_start_settings(name+prologue+start_situation) → STORY {{start_setting}} 통글.

    같은 세계관이어도 시작 설정이 다르면 전개가 갈리므로, 이 플레이의 출발점을 매 턴
    STORY 슬롯에 고정 삽입한다(명세 3.3). 첫 턴 History 시드(백엔드 담당)와 병행한다.
    """
    prologue = strip_character_image_syntax(start.prologue)
    start_situation = strip_character_image_syntax(start.start_situation)
    return f"# 시작 설정: {start.name}\n\n{prologue}\n\n{start_situation}"


def format_main_events(events: list[MainEvent]) -> str:
    """주요 사건 목록 → 슬롯 텍스트. 선택지 재료(chat_choices)와 공용이다."""
    return "\n".join(
        f"- 이름: {e.name} / 설명: {e.description} / 키 문장: {e.key_sentence}" for e in events
    ) or "(없음)"


def format_target_main_event(target: TargetMainEvent | None) -> str:
    """목표 사건 상태 → 슬롯 텍스트. 목표 없음도 명시 문구로 넣는다(빈 칸 금지)."""
    return f"이름: {target.name} (진행 {target.progress_turns}턴)" if target else "(없음)"


def _format_endings(endings: list[EndingCandidate]) -> str:
    """도달 후보 엔딩 → STORY 슬롯 텍스트.

    epilogue를 포함한다 — 본문 호출이 엔딩 응답 생성 여부를 스스로 판단하고 도달 시
    에필로그 가이드를 반영해야 하기 때문(판정 재료와 달리 출력 가이드까지 필요).
    """
    return "\n".join(
        f"- 이름: {e.name} / 달성 조건: {e.achievement_condition} / 에필로그 가이드: {e.epilogue}"
        for e in endings
    ) or "(없음)"


# `_slot_map`이 채우는 자리표시자. general 템플릿 검사(`_check_general_slots`)가 쓰며,
# 둘이 어긋나지 않는지는 테스트가 확인한다.
_SLOT_KEYS = (
    "{{장르}}",
    "{{world_setting}}",
    "{{start_setting}}",
    "{{rule_setting}}",
    "{{character_setting}}",
    "{{user_role_setting}}",
    "{{main_events}}",
    "{{target_main_event}}",
    "{{endings}}",
    "{{protagonist_name}}",
)


def _slot_map(req: ChatTurnRequest) -> dict[str, str]:
    """ChatTurnRequest → 정적 레이어 슬롯 치환 맵(결정적, LLM 없음).

    장르는 `stories.genre`, start_setting은 `story_start_settings`에서 오는 예외 소스다
    (나머지는 story_settings 통글 4필드, 명세 3.3). 사건·엔딩 슬롯 3종(KNK-485)은
    재료가 비면 "(없음)" 문구로 치환된다. 요청의 `character_images`는 프롬프트에 넣지
    않는다 — 이미지는 AI 서버가 출력의 `인물명:` 줄을 찾아 붙이므로 LLM이 알 필요가 없다(KNK-1006).
    """
    ss = req.story_settings
    return {
        "{{장르}}": req.genre,
        "{{world_setting}}": ss.world_setting,
        "{{start_setting}}": _start_setting_blob(req.start_settings),
        "{{rule_setting}}": ss.rule_setting,
        "{{character_setting}}": ss.character_setting,
        "{{user_role_setting}}": ss.user_role_setting,
        "{{main_events}}": format_main_events(req.main_events),
        "{{target_main_event}}": format_target_main_event(req.target_main_event),
        "{{endings}}": _format_endings(req.endings),
        "{{protagonist_name}}": ss.protagonist_name,
    }


def _apply(body: str, repl: dict[str, str]) -> str:
    for k, v in repl.items():
        body = body.replace(k, v)
    return body


def _system_front(repl: dict[str, str]) -> str:
    """SAFETY→CORE→STORY→CHARACTER→USER를 슬롯 치환해 [시스템 앞] 블록으로 합친다."""
    return "\n\n".join(_apply(_LAYERS[n], repl) for n in _SYSTEM_FRONT)


def _history_messages(history: list[ChatHistoryItem]) -> list[dict]:
    """History를 LLM 호출용 messages로 변환한다(대문자 role → 소문자).

    SYSTEM은 스키마(Literal)상 들어올 수 없고 백엔드도 제외해 보낸다(명세 4.2).
    """
    return [
        {"role": _ROLE_MAP[h.role], "content": strip_character_image_syntax(h.content)}
        for h in history
    ]


def _depth_block(summary: str) -> str:
    """MEMORY 본문에 받은 summary를 치환해 [Depth] 블록을 만든다.

    summary가 빈 문자열이면 [현재 상태]가 빈 칸으로 남는다(명세 5.1). 요약 생성은
    백엔드 몫(Phase 4)이며 AI는 받은 summary를 참조만 한다 — MVP에서는 summary가
    비어 있고 맥락은 History가 담당한다.
    """
    return _LAYERS["MEMORY"].replace("{{summary}}", summary)


def _phi_block() -> str:
    """[PHI] 재주입 블록 — CHARACTER→STORY→CORE→SAFETY의 phi_core만(명세 4.1)."""
    return "\n\n".join(f"# {n}\n{_phi_core(_LAYERS[n])}" for n in _PHI_ORDER)


# ── general 프롬프트 (KNK-1622) ─────────────────────────────────────────────
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")
# general에서만 쓰는 슬롯. layer는 summary를 MEMORY로, user_input을 별도 user 턴으로 넣지만
# general은 둘 다 [USER] 블록 안에 둔다.
_GENERAL_ONLY_SLOTS = ("{{summary}}", "{{user_input}}")


def _split_general(path: Path) -> tuple[str, str]:
    """general 템플릿을 `## [SYSTEM]`/`## [USER]` 두 블록으로 나눈다. 마커가 없으면 기동 실패."""
    try:
        body = _body_only(path.read_text(encoding="utf-8-sig"))
        _, after_system = body.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"general 프롬프트 로드/파싱 실패: {path.name}: {e}") from e
    return system_raw.strip(), user_raw.strip()


def _check_general_slots(system: str, user: str) -> None:
    """템플릿의 자리표시자가 모두 채울 수 있는 슬롯인지, 입력 슬롯이 [USER]에 있는지 확인한다.

    모르는 자리표시자는 치환되지 않고 `{{...}}` 그대로 모델에 가고, `{{user_input}}`이
    [USER]에 없으면 사용자 입력이 통째로 빠진다. 둘 다 오류 없이 응답 품질만 무너지므로
    모듈 로드 시점(서버 기동)에 막는다.
    """
    known = set(_SLOT_KEYS) | set(_GENERAL_ONLY_SLOTS)
    unknown = set(_PLACEHOLDER_RE.findall(system + user)) - known
    if unknown:
        raise RuntimeError(f"general 프롬프트에 채울 수 없는 자리표시자: {sorted(unknown)}")
    if "{{user_input}}" not in user:
        raise RuntimeError("general 프롬프트 [USER]에 {{user_input}}이 없습니다.")


def _render_once(body: str, repl: dict[str, str]) -> str:
    """자리표시자를 한 번에 훑어 치환한다(`prompt._render`와 같은 이유).

    순차 .replace()는 앞서 넣은 값(사용자 입력·설정 본문)을 다음 치환이 다시 검사해,
    값 안에 `{{summary}}` 같은 글자가 있으면 엉뚱한 내용으로 바뀐다. 한 번에 훑으면
    넣은 값은 다시 검사하지 않는다.
    """
    return _PLACEHOLDER_RE.sub(lambda m: repl.get(m.group(0), m.group(0)), body)


def _assemble_general(req: ChatTurnRequest) -> list[dict]:
    """general: [SYSTEM] → [History] → [USER](현재 상태·목표 사건·이번 입력).

    매 턴 바뀌는 값은 모두 마지막 user 메시지에 있어, 앞부분(지시문+스토리 설정)은 같은
    채팅 안에서 대부분의 턴 동안 그대로다 — 공급자의 프롬프트 캐시가 앞부분을 재사용할 수 있다.
    예외는 엔딩 후보(`{{endings}}`)다. 백엔드가 최소 턴을 채운 후보만 보내고 도달 뒤엔 비워
    보내므로, 그 턴에는 앞부분이 바뀌어 캐시가 새로 만들어진다.
    """
    repl = _slot_map(req) | {"{{summary}}": req.summary, "{{user_input}}": req.user_input}
    messages: list[dict] = [{"role": "system", "content": _render_once(_GENERAL_SYSTEM, repl)}]
    messages.extend(_history_messages(req.history))
    messages.append({"role": "user", "content": _render_once(_GENERAL_USER, repl)})
    return messages


def assemble(req: ChatTurnRequest, prompt: ChatPrompt | None = None) -> list[dict]:
    """시점 B: ChatTurnRequest를 본문 프롬프트 messages로 조립한다(완전 stateless).

    `prompt`를 주지 않으면 설정 `CHAT_PROMPT`를 따른다. 인자는 평가 도구처럼 한 프로세스에서
    두 프롬프트를 번갈아 조립해야 하는 쪽을 위한 것이다.

    layer 배치(명세 4.1): [시스템 앞] → [History] → user_input → [Depth: 현재 상태] → [PHI].
    user_input은 history(과거 대화)와 별도로 전달되므로 마지막 user 턴으로 붙인다.
    Depth·PHI는 끝 근처에 system 역할로 둔다 — system vs user 역할 분기는 Phase 5에서
    검증 후 확정한다(명세 4.3; Anthropic 어댑터 시 Depth를 user 역할로 분기).
    """
    if _selected_prompt(prompt) == "general":
        return _assemble_general(req)
    repl = _slot_map(req)
    messages: list[dict] = [{"role": "system", "content": _system_front(repl)}]
    messages.extend(_history_messages(req.history))
    messages.append({"role": "user", "content": req.user_input})
    messages.append({"role": "system", "content": _depth_block(req.summary)})
    messages.append({"role": "system", "content": _phi_block()})
    return messages



# general 템플릿은 정적이라 모듈 로드 시 1회만 읽고 검사한다(layer 캐시와 같은 이유).
_GENERAL_SYSTEM, _GENERAL_USER = _split_general(_GENERAL_PATH)
_check_general_slots(_GENERAL_SYSTEM, _GENERAL_USER)
