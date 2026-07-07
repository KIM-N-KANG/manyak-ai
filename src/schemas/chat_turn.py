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

from src.schemas.response_meta import ChatResponseMeta


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


class ChatStartSettings(BaseModel):
    """시작 설정 — 이 플레이의 출발점이자 전개 방향을 정하는 전제. STORY 레이어 슬롯 재료.

    `story_start_settings` 테이블에서 온다. `world_setting`(여러 플레이가 공유하는
    공통 세계관)과 달리, 시작 설정은 **이 플레이 고유의 진입점**이다. 같은 세계관이어도
    시작 설정이 다르면 스토리가 다르게 흘러가므로(명세상 world_setting의 갈등은
    '일어날 수 있는 가능성'이고 start_situation은 그 갈등으로 들어가는 구체적 진입점),
    매 턴 프롬프트(STORY `{{start_setting}}` 슬롯)에 고정 삽입해 모델이 전개 방향을
    그 시작점에 맞춰 잡게 한다. 슬롯은 **입력(모델이 읽는 배경)**이지 출력이 아니다 —
    이 값이 aiOutput으로 그대로 나오지 않는다.

    조립기(T2)가 name·prologue·start_situation을 통글로 엮어 슬롯에 주입한다. name은
    UI용 이름이지만 시작 설정의 정체성을 드러내므로 슬롯 재료에 포함한다.
    """

    name: str
    prologue: str
    start_situation: str


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


class MainEvent(BaseModel):
    """주요 사건 — 이야기의 갈림길(스토리와 1:N, 최대 10).

    컴파일 산출물(`StoryMainEventOut`)·일반 제작 저작분과 동일 구조로 백엔드
    `story_main_events`에서 온다. key_sentence는 사용자 입력이 이 사건과 의미상
    관련되는지 판단하는 기준 문장이며, 관련성 판정은 AI의 정성 판정이다(D11 —
    5-ai-server §5-3-4).
    """

    name: str
    description: str
    key_sentence: str


class TargetMainEvent(BaseModel):
    """목표 사건 상태 — 이 채팅이 현재 향해 진행 중인 주요 사건(채팅당 최대 1개).

    AI는 무상태(D2)라 직전 턴 completed 메타(`targetMainEvent`)를 백엔드가
    저장했다가 매 턴 되돌려 싣는다. progress_turns는 목표를 향해 진행한 턴 수이며
    이탈 턴에는 동결된다(증가 판정도 AI 몫 — §5-3-4 진행 규칙).
    """

    name: str
    progress_turns: int = Field(ge=0)


class EndingCandidate(BaseModel):
    """도달 후보 엔딩 — 달성 조건(자연어) 정성 판정의 재료.

    min_turns가 없는 이유: 최소 턴 수는 결정적 판정이라 백엔드가 충족한 후보만
    걸러 싣는다(D11 분담). 이미 도달한 채팅이면 endings 자체가 빈 배열로 와서
    재판정이 차단된다(도달 인정은 채팅당 최초 1회 — 4-backend §4-3-10).
    """

    name: str
    achievement_condition: str
    epilogue: str


class ChatTurnRequest(BaseModel):
    """채팅 턴 API 입력 (매 턴, 완전 stateless).

    session_id를 두지 않는다 — AI는 무상태라 식별자로 조회·분기할 게 없고, 대화를
    묶는 책임은 백엔드(chatId)에 있다. genre는 `stories.genre`에서 직접 치환된다
    (명세 3.3 예외 경로). story_settings(공통 세계관·인물·규칙)와 start_settings(이
    플레이 고유의 시작점)는 둘 다 STORY/CHARACTER/USER 정적 레이어 슬롯 재료다.
    history는 백엔드가 최근 10턴 윈도우로 잘라 전달하고, summary는 채팅별 메모리
    TEXT(대화 요약)로 매 턴 받아 참조한다(메모리 참조는 채팅 기능의 일부이며, 요약
    생성은 Phase 4 별개 기능이다).
    """

    genre: str
    story_settings: ChatStorySettings
    start_settings: ChatStartSettings
    history: list[ChatHistoryItem] = Field(default_factory=list)
    user_input: str
    # MEMORY(대화 요약) — 채팅별로 하나씩 존재하는 요약 문자열(특정 채팅 수마다 압축·최신화).
    # 메모리 참조는 채팅 기능의 일부이므로 매 턴 받아 조립기가 Depth(`[현재 상태]`)에 그대로
    # 넣는다(빈 문자열이면 빈 칸 그대로). 메모리를 요약·기록(생성)하는 로직은 별개 기능(Phase 4)
    # 이라, 그전까지 백엔드는 빈 문자열을 보낸다.
    summary: str
    # ── 주요 사건·엔딩 진행 재료 (KNK-482, 5-ai-server §5-3-4·D11) ──────────────
    # 넷 다 선택 필드다 — 백엔드 런타임 전달(4-backend §4-3-10)이 미구현이라, 재료가
    # 없으면 기존 요청과 동일하게 동작해야 한다(하위호환·AI 선행 배포). 재료가 실리면
    # 프롬프트 판정 재료와 completed 판정 메타의 입력이 된다.
    main_events: list[MainEvent] = Field(default_factory=list, max_length=10)
    target_main_event: TargetMainEvent | None = None
    occurred_main_event_names: list[str] = Field(default_factory=list)
    endings: list[EndingCandidate] = Field(default_factory=list)


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


class TargetMainEventOut(BaseModel):
    """completed 판정 메타 — 이번 턴 판정 후 목표 사건 상태.

    백엔드가 저장했다가 다음 턴 요청(`target_main_event`)에 되돌려 싣는다(D11 —
    상태 보관은 백엔드, 관측과 같은 '응답 계약의 일부' 방식). 와이어는 camelCase.
    """

    name: str
    progress_turns: int = Field(ge=0, serialization_alias="progressTurns")


class CompletedData(BaseModel):
    """event: completed — 한 턴 응답 완료.

    엔드포인트가 본문 호출(상황·대사)과, 본문과 분리된 선택지 호출(다음 행동 3개)의 결과를 합쳐 두 필드로 담는다.

    - aiOutput: 본문(상황 묘사 + 인물 대사)만. **선택지는 제외**한다. 백엔드가
      chatId·turnId를 더해 이 값만 DB(history)에 저장한다 — 선택지는 '제안된
      가능성'이라 실제 진행만 담는 History에 남기지 않는다(실제 진행은 사용자가 고른
      다음 user 입력이다).
    - choices: 다음 행동 선택지 3개(본문과 분리된 별도 호출이 코드로 정확히 3개 보장). UI
      버튼용이며 History에는 들어가지 않는다.
    """

    # aiOutput은 manyak-server SSE 와이어 계약 키(camelCase, server ChatService가 SSOT)라
    # 유지하되, 스타일 가이드(.gemini/styleguide.md:42 snake_case)에 맞춰 필드명은 ai_output으로
    # 둔다. ⚠️ 직렬화 시 반드시 model_dump(by_alias=True)를 써야 와이어가 aiOutput이 된다(T3).
    ai_output: str = Field(serialization_alias="aiOutput")
    choices: list[str]
    # 로깅 메타(KNK-243). completed 이벤트에만 실리며 엔드포인트가 항상 채운다.
    meta: ChatResponseMeta | None = None
    # ── 주요 사건·엔딩 판정 메타 (KNK-483, §5-3-4·D11) ─────────────────────────
    # 백엔드가 턴 저장 트랜잭션에서 채팅 상태로 반영한다(목표 갱신·사건 완결 기록·
    # 엔딩 도달 해소). 재료가 없거나 판정이 실패하면 셋 다 null — 판정 실패가 턴을
    # 깨지 않는다(선택지 폴백과 같은 원칙). 서버 DTO는 ignoreUnknown이라 선행 배포 안전.
    target_main_event: TargetMainEventOut | None = Field(
        default=None, serialization_alias="targetMainEvent"
    )
    occurred_main_event_name: str | None = Field(
        default=None, serialization_alias="occurredMainEventName"
    )
    ending_name: str | None = Field(default=None, serialization_alias="endingName")


class ErrorData(BaseModel):
    """event: error — 스트림 실패. 코드·메시지로 원인을 전달한다."""

    code: str
    message: str
