"""채팅 턴 API(시점 B 런타임)의 입출력 계약.

reference/chat/4-SERVICE-IMPLEMENTATION.md 기준. 단일 채팅 턴 API는 매 턴
백엔드가 모든 재료를 실어 보내고 AI는 받은 것만으로 응답하는 **완전 stateless**
구조다. AI는 턴 사이에 아무것도 보관하지 않으며, 세션 식별자도 두지 않는다.

- 입력: 백엔드 → AI (매 턴) — ChatTurnRequest
- 출력: AI → 백엔드 (SSE 스트림). AI는 token·completed·error만 발행하고,
        started·chatId·turnId는 백엔드가 발행·부착한다(manyak-server 규격과 통일).
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

    role=ASSISTANT: AI 출력 또는 **오프닝 시드**(prologue+start_situation).
    role=USER: 사용자 입력.

    role 값은 ERD `story_messages.role`(대문자 enum)과 맞춘다. 단 OpenAI 호환 LLM은
    소문자 role(user/assistant)을 요구하므로, **조립기가 LLM 호출 직전 소문자로 변환**한다.
    SYSTEM은 history에 넣지 않는다(시스템 프롬프트는 조립기가 따로 구성).

    오프닝 시드는 백엔드가 작가의 prologue·start_situation을 `*…*` 지문으로 래핑해
    history 첫 항목으로 깔아 보낸다(명세 2.5 표기 규약). AI는 첫 턴/이후 턴을
    구분하지 않고 받은 history를 그대로 조립한다.
    """

    role: Literal["USER", "ASSISTANT"]
    content: str


class ChatTurnRequest(BaseModel):
    """채팅 턴 API 입력 (매 턴, 완전 stateless).

    session_id를 두지 않는다 — AI는 무상태라 식별자로 조회·분기할 게 없고, 대화를
    묶는 책임은 백엔드(chatId)에 있다. genre는 `stories.genre`에서 직접 치환된다
    (명세 3.3 예외 경로). history는 백엔드가 최근 10턴 윈도우로 잘라 전달하고,
    summary는 채팅별 메모리 TEXT(대화 요약)로 매 턴 받아 참조한다(메모리 참조는 채팅
    기능의 일부이며, 요약 생성은 Phase 4 별개 기능이다).
    """

    genre: str
    story_settings: ChatStorySettings
    history: list[ChatHistoryItem] = Field(default_factory=list)
    user_input: str
    # MEMORY(대화 요약) — 채팅별로 하나씩 존재하는 메모리 TEXT(특정 채팅 수마다 압축·최신화).
    # 메모리 참조는 채팅 기능의 일부이므로 매 턴 받아 조립기가 Depth(`[현재 상태]`)에 그대로
    # 넣는다(빈 문자열이면 빈 칸 그대로). 메모리를 요약·기록(생성)하는 로직은 별개 기능(Phase 4)
    # 이라, 그전까지 백엔드는 빈 문자열을 보낸다.
    summary: str


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
    """event: completed — 한 턴 응답 완료.

    AI가 LLM 통짜 출력(상황·대사 + `[다음 행동]` 3개)을 갈라 두 필드로 나눠 담는다.

    - aiOutput: 본문(상황 묘사 + 인물 대사)만. **선택지는 제외**한다. 백엔드가
      chatId·turnId를 더해 이 값만 DB(history)에 저장한다 — 선택지는 '제안된
      가능성'이라 실제 진행만 담는 History에 남기지 않는다(실제 진행은 사용자가 고른
      다음 user 입력이다).
    - choices: 다음 행동 선택지 3개(CORE 봉투가 보장). UI 버튼용이며 History에는
      들어가지 않는다.
    """

    aiOutput: str
    choices: list[str]


class ErrorData(BaseModel):
    """event: error — 스트림 실패. 코드·메시지로 원인을 전달한다."""

    code: str
    message: str
