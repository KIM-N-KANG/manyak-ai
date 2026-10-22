"""채팅 요청을 단일 템플릿의 시스템 → 대화 기록 → 사용자 메시지로 조립한다.

AI는 상태를 보관하지 않으며 매 턴 받은 요청으로 메시지를 구성한다.
LLM 호출과 SSE 응답은 별도 모듈에서 담당한다.
"""

import re
from pathlib import Path

from src.schemas.chat_turn import (
    ChatHistoryItem,
    ChatStartSettings,
    ChatTurnRequest,
    EndingCandidate,
    MainEvent,
)
from src.services.chat_image_markers import strip_character_image_syntax
from src.services.prompt_meta import read_version

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "prompt" / "chat" / "CHAT-TEMPLATE.md"
CHAT_VERSION = read_version(_TEMPLATE_PATH)


def body_prompt_versions() -> dict[str, int]:
    """사용한 본문 템플릿의 버전을 새 맵으로 반환한다."""
    return {"CHAT": CHAT_VERSION}


def _body_only(text: str) -> str:
    """frontmatter(--- ... ---)를 떼고 본문만 반환한다(조립기는 content만 주입).

    줄바꿈은 LF/CRLF 모두 허용한다(read_version과 동일 관례) — CRLF로 저장된
    템플릿에서 frontmatter가 본문에 새어 LLM에 전달되는 사고를 막는다(KNK-574).
    """
    m = re.match(r"^---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n(.*)$", text, re.S)
    return (m.group(1) if m else text).strip()


# OpenAI 호환 LLM은 소문자 role을 요구한다(ERD/스키마는 대문자 USER/ASSISTANT).
_ROLE_MAP = {"USER": "user", "ASSISTANT": "assistant"}


def _start_setting_blob(start: ChatStartSettings) -> str:
    """story_start_settings(name+prologue+start_situation) → 시작 설정 슬롯 텍스트.

    같은 세계관이어도 시작 설정이 다르면 전개가 갈리므로, 이 플레이의 출발점을 매 턴
    시작 설정 슬롯에 고정 삽입한다. 첫 턴 대화 기록(백엔드 담당)과 병행한다.
    """
    prologue = strip_character_image_syntax(start.prologue)
    start_situation = strip_character_image_syntax(start.start_situation)
    return f"# 시작 설정: {start.name}\n\n{prologue}\n\n{start_situation}"


def format_main_events(events: list[MainEvent]) -> str:
    """주요 사건 목록 → 슬롯 텍스트. 선택지 재료(chat_choices)와 공용이다."""
    return "\n".join(
        f"- 이름: {e.name} / 설명: {e.description} / 키 문장: {e.key_sentence}" for e in events
    ) or "(없음)"


def _format_endings(endings: list[EndingCandidate]) -> str:
    """도달 후보 엔딩 → 사용자 메시지의 엔딩 슬롯 텍스트.

    epilogue를 포함한다 — 본문 호출이 엔딩 응답 생성 여부를 스스로 판단하고 도달 시
    에필로그 가이드를 반영해야 하기 때문.
    """
    return "\n".join(
        f"- 이름: {e.name} / 달성 조건: {e.achievement_condition} / 에필로그 가이드: {e.epilogue}"
        for e in endings
    ) or "(없음)"


# `_slot_map`이 채우는 자리표시자. 템플릿 검사(`_check_slots`)가 쓰며,
# 둘이 어긋나지 않는지는 테스트가 확인한다.
_SLOT_KEYS = (
    "{{장르}}",
    "{{world_setting}}",
    "{{start_setting}}",
    "{{rule_setting}}",
    "{{character_setting}}",
    "{{user_role_setting}}",
    "{{main_events}}",
    "{{endings}}",
    "{{protagonist_name}}",
)


def _slot_map(req: ChatTurnRequest) -> dict[str, str]:
    """ChatTurnRequest → 스토리 설정과 사건·엔딩 슬롯 치환 맵.

    장르는 `stories.genre`, start_setting은 `story_start_settings`에서 오는 예외 소스다
    (나머지는 story_settings 통글 4필드, 명세 3.3). 사건·엔딩 슬롯 2종(KNK-485)은
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
        "{{endings}}": _format_endings(req.endings),
        "{{protagonist_name}}": ss.protagonist_name,
    }


def _history_messages(history: list[ChatHistoryItem]) -> list[dict]:
    """History를 LLM 호출용 messages로 변환한다(대문자 role → 소문자).

    SYSTEM은 스키마(Literal)상 들어올 수 없고 백엔드도 제외해 보낸다(명세 4.2).
    """
    return [
        {"role": _ROLE_MAP[h.role], "content": strip_character_image_syntax(h.content)}
        for h in history
    ]


_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")
_USER_SLOTS = ("{{summary}}", "{{user_input}}")


def _load_template(path: Path) -> tuple[str, str]:
    """채팅 템플릿을 `## [SYSTEM]`/`## [USER]` 두 블록으로 나눈다. 마커가 없으면 기동 실패."""
    try:
        body = _body_only(path.read_text(encoding="utf-8-sig"))
        _, after_system = body.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"채팅 프롬프트 로드/파싱 실패: {path.name}: {e}") from e
    return system_raw.strip(), user_raw.strip()


def _check_slots(system: str, user: str) -> None:
    """템플릿의 자리표시자가 모두 채울 수 있는 슬롯인지, 입력 슬롯이 [USER]에 있는지 확인한다.

    모르는 자리표시자는 치환되지 않고 `{{...}}` 그대로 모델에 가고, `{{user_input}}`이
    [USER]에 없으면 사용자 입력이 통째로 빠진다. 둘 다 오류 없이 응답 품질만 무너지므로
    모듈 로드 시점(서버 기동)에 막는다.
    """
    known = set(_SLOT_KEYS) | set(_USER_SLOTS)
    unknown = set(_PLACEHOLDER_RE.findall(system + user)) - known
    if unknown:
        raise RuntimeError(f"채팅 프롬프트에 채울 수 없는 자리표시자: {sorted(unknown)}")
    if "{{user_input}}" not in user:
        raise RuntimeError("채팅 프롬프트 [USER]에 {{user_input}}이 없습니다.")


def _render_once(body: str, repl: dict[str, str]) -> str:
    """자리표시자를 한 번에 훑어 치환한다(`prompt._render`와 같은 이유).

    순차 .replace()는 앞서 넣은 값(사용자 입력·설정 본문)을 다음 치환이 다시 검사해,
    값 안에 `{{summary}}` 같은 글자가 있으면 엉뚱한 내용으로 바뀐다. 한 번에 훑으면
    넣은 값은 다시 검사하지 않는다.
    """
    return _PLACEHOLDER_RE.sub(lambda m: repl.get(m.group(0), m.group(0)), body)


def assemble(req: ChatTurnRequest) -> list[dict]:
    """시스템 → 대화 기록 → 사용자 메시지(엔딩 후보·요약·이번 입력)로 조립한다."""
    repl = _slot_map(req) | {"{{summary}}": req.summary, "{{user_input}}": req.user_input}
    messages: list[dict] = [{"role": "system", "content": _render_once(_SYSTEM, repl)}]
    messages.extend(_history_messages(req.history))
    messages.append({"role": "user", "content": _render_once(_USER, repl)})
    return messages


# 템플릿은 기동 시 한 번 읽고 검사한다.
_SYSTEM, _USER = _load_template(_TEMPLATE_PATH)
_check_slots(_SYSTEM, _USER)
