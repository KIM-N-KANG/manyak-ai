"""채팅 턴 API(시점 B 런타임)의 입출력 계약.

reference/chat/4-SERVICE-IMPLEMENTATION.md 기준. 단일 채팅 턴 API는 매 턴
백엔드가 모든 재료를 실어 보내고 AI는 받은 것만으로 응답하는 **완전 stateless**
구조다. AI는 턴 사이에 아무것도 보관하지 않으며, 세션 식별자도 두지 않는다.

- 입력: 백엔드 → AI (매 턴) — ChatTurnRequest
- 출력: AI → 백엔드 (SSE 스트림) — manyak-server SSE 규격과 통일
        (started → token → completed → error)
"""

from typing import Literal

from pydantic import BaseModel, Field


# ── 입력 (백엔드 → AI, 매 턴) ───────────────────────────────────────────────


class ChatStorySettings(BaseModel):
    """프롬프트 슬롯 재료 — `story_settings` 통글 마크다운 4필드.

    스토리 컴파일(A-1)의 산출물(`StorySettingsOut`)과 동일 구조다. 채팅 턴마다
    백엔드가 DB에서 조회해 그대로 전달하며, AI는 보관하지 않고 매 턴 슬롯에 통째로
    치환해 쓴다(완전 stateless). 슬롯 매핑은 명세 3.3:
    world_setting→{{world_setting}}, rule_setting→{{rule_setting}},
    character_setting→{{character_setting}}, user_role_setting→{{user_role_setting}}.
    """

    world_setting: str
    character_setting: str
    user_role_setting: str
    rule_setting: str


class ChatHistoryItem(BaseModel):
    """대화 기록 한 줄.

    role=assistant: AI 출력 또는 **오프닝 시드**(prologue+start_situation).
    role=user: 사용자 입력.

    오프닝 시드는 백엔드가 작가의 prologue·start_situation을 `*…*` 지문으로 래핑해
    history 첫 항목으로 깔아 보낸다(명세 2.5 표기 규약). AI는 첫 턴/이후 턴을
    구분하지 않고 받은 history를 그대로 조립한다.
    """

    role: Literal["user", "assistant"]
    content: str


class ChatTurnRequest(BaseModel):
    """채팅 턴 API 입력 (매 턴, 완전 stateless).

    session_id를 두지 않는다 — AI는 무상태라 식별자로 조회·분기할 게 없고, 대화를
    묶는 책임은 백엔드(chatId)에 있다. genre는 `stories.genre`에서 직접 치환된다
    (명세 3.3 예외 경로). history는 백엔드가 최근 10턴 윈도우로 잘라 전달한다.
    """

    genre: str
    story_settings: ChatStorySettings
    history: list[ChatHistoryItem] = Field(default_factory=list)
    user_input: str


# ── 출력 (AI → 백엔드, SSE 스트림) ──────────────────────────────────────────
# manyak-server의 SSE 규격(started→token→completed→error)과 통일한다.
# AI(manyak-ai)가 발행하는 이벤트는 token·completed·error 3개뿐이다.
# started는 백엔드가 SSE 스트림을 열며 자체 발행한다(chatId 신호 — AI 미발행).
# chatId·turnId도 백엔드가 부착하므로 AI 페이로드에는 넣지 않는다.

EVENT_TOKEN = "token"
EVENT_COMPLETED = "completed"
EVENT_ERROR = "error"


class TokenData(BaseModel):
    """event: token — 생성 토큰(델타) 한 조각."""

    text: str


class CompletedData(BaseModel):
    """event: completed — 누적 완성본 전체. 백엔드가 chatId·turnId를 더해 DB에 저장한다."""

    aiOutput: str


class ErrorData(BaseModel):
    """event: error — 스트림 실패. 코드·메시지로 원인을 전달한다."""

    code: str
    message: str
