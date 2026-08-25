"""채팅 6레이어 프롬프트 조립 (시점 B, 매 턴, 완전 stateless).

`ChatTurnRequest`를 받아 정적 레이어 슬롯을 결정적 치환하고, 명세 4.1 배치
([시스템 앞] → [History] → [Depth] → [PHI])로 LLM 호출용 messages 배열을 만든다.
AI는 상태를 보관하지 않으며(순수 함수), 모든 재료는 매 턴 request로 받는다.

LLM 호출(`stream=True`)·SSE 엔드포인트는 별도(KNK-144)다. 이 모듈은 조립까지만 한다.
`scripts/preview_chat_prompt.py`가 시각 검증용 reference이며 동일 로직을 따른다.
"""

import re
from pathlib import Path

from src.schemas.chat_turn import (
    CharacterImageMapping,
    ChatHistoryItem,
    ChatStartSettings,
    ChatTurnRequest,
    EndingCandidate,
    MainEvent,
    TargetMainEvent,
)
from src.services.prompt_meta import read_version

_CHAT_DIR = Path(__file__).parent.parent.parent / "prompt" / "chat"
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
    return _CHAT_DIR / f"{name}-TEMPLATE.md"


def _load_layer(name: str) -> str:
    # utf-8-sig: BOM이 있으면 떼고 읽는다 — frontmatter가 본문에 새지 않게.
    return _body_only(_layer_path(name).read_text(encoding="utf-8-sig"))


# 레이어 본문은 정적이라 모듈 로드 시 1회만 읽어 캐시한다(매 턴 재치환은 .replace로 충분).
_LAYERS = {n: _load_layer(n) for n in _LAYER_NAMES}

# 레이어 버전은 frontmatter가 SSOT다(KNK-228). 로깅용으로 레이어별 버전을 노출한다(KNK-243).
LAYER_VERSIONS = {n: read_version(_layer_path(n)) for n in _LAYER_NAMES}

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
    return f"# 시작 설정: {start.name}\n\n{start.prologue}\n\n{start.start_situation}"


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


def format_character_image_names(images: list[CharacterImageMapping]) -> str:
    """이미지 보유 인물 목록 → CHARACTER 슬롯 텍스트(URL은 LLM에 전달하지 않음)."""
    return "\n".join(f"- {image.name}" for image in images) or "(없음)"


def _slot_map(req: ChatTurnRequest) -> dict[str, str]:
    """ChatTurnRequest → 정적 레이어 슬롯 치환 맵(결정적, LLM 없음).

    장르는 `stories.genre`, start_setting은 `story_start_settings`에서 오는 예외 소스다
    (나머지는 story_settings 통글 4필드, 명세 3.3). 사건·엔딩 슬롯 3종(KNK-485)과
    이미지 보유 인물 목록(KNK-990)은 재료가 비면 "(없음)" 문구로 치환된다.
    """
    ss = req.story_settings
    return {
        "{{장르}}": req.genre,
        "{{world_setting}}": ss.world_setting,
        "{{start_setting}}": _start_setting_blob(req.start_settings),
        "{{rule_setting}}": ss.rule_setting,
        "{{character_setting}}": ss.character_setting,
        "{{character_image_names}}": format_character_image_names(req.character_images),
        "{{user_role_setting}}": ss.user_role_setting,
        "{{main_events}}": format_main_events(req.main_events),
        "{{target_main_event}}": format_target_main_event(req.target_main_event),
        "{{endings}}": _format_endings(req.endings),
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
    return [{"role": _ROLE_MAP[h.role], "content": h.content} for h in history]


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


def assemble(req: ChatTurnRequest) -> list[dict]:
    """시점 B: ChatTurnRequest를 6레이어 프롬프트 messages로 조립한다(완전 stateless).

    배치(명세 4.1): [시스템 앞] → [History] → user_input → [Depth: 현재 상태] → [PHI].
    user_input은 history(과거 대화)와 별도로 전달되므로 마지막 user 턴으로 붙인다.
    Depth·PHI는 끝 근처에 system 역할로 둔다 — system vs user 역할 분기는 Phase 5에서
    검증 후 확정한다(명세 4.3; Anthropic 어댑터 시 Depth를 user 역할로 분기).
    """
    repl = _slot_map(req)
    messages: list[dict] = [{"role": "system", "content": _system_front(repl)}]
    messages.extend(_history_messages(req.history))
    messages.append({"role": "user", "content": req.user_input})
    messages.append({"role": "system", "content": _depth_block(req.summary)})
    messages.append({"role": "system", "content": _phi_block()})
    return messages
